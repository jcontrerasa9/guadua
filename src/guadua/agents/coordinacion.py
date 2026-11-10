"""Agente 6: Coordinación de respuesta y asignación de recursos.

Consulta la matriz EMRE de Manizales para determinar la entidad responsable,
verifica la disponibilidad de recursos y redacta la ficha de coordinación.
La ficha queda pendiente hasta la aprobación humana (decisión del operador).
CONTRATO: genera y valida eventos `ficha` (docs/contratos.md §Catálogo).
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from guadua.knowledge.matriz_emre import cita_por_tipo, recursos_disponibles, responsable_por_tipo
from guadua.knowledge.retriever import Passage, Retriever
from guadua.protocol.header import Incident, People
from guadua.providers.llm import LLMProvider, Message
from guadua.store.event_types import (
    Citation,
    Decision,
    Ficha,
    IncidenteAgrupado,
    ReporteNormalizado,
    validate_event,
)
from guadua.store.events import Event, EventCategory, EventId, EventStore

_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
_RIESGOS_CSV = _DEFAULT_DATA_DIR / "riesgos_unidad.csv"

logger = logging.getLogger("guadua.coordinacion")


@dataclass(frozen=True, slots=True)
class EntradaRiesgo:
    senal_riesgo: str
    advertencia: str
    unidad_recomendada: str
    es_ejemplo: bool


def cargar_riesgos_unidad(ruta_csv: Path | str | None = None) -> dict[str, EntradaRiesgo]:
    """Carga el catálogo de riesgos en escena y unidades recomendadas."""
    path = Path(ruta_csv) if ruta_csv else _RIESGOS_CSV
    if not path.is_file():
        path = Path("data/riesgos_unidad.csv")
    if not path.is_file():
        return {}

    riesgos: dict[str, EntradaRiesgo] = {}
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            senal = row["senal_riesgo"].strip()
            riesgos[senal] = EntradaRiesgo(
                senal_riesgo=senal,
                advertencia=row["advertencia"].strip(),
                unidad_recomendada=row["unidad_recomendada"].strip(),
                es_ejemplo=row["es_ejemplo"].strip().lower() in ("true", "1", "yes"),
            )
    return riesgos


def _construir_recomendacion_riesgo(
    unidades_riesgo: Sequence[str],
    responsable: str | None,
    tipo: Incident,
) -> str:
    """Construye la recomendación operativa de unidad especializada ante riesgos en escena."""
    if not unidades_riesgo:
        return ""
    unidades_str = " y ".join(unidades_riesgo)
    if tipo == Incident.HERIDO or responsable == "Secretaría de Salud":
        return f"{unidades_str} asegura la escena antes de enviar ambulancia estándar."
    destino = responsable if responsable else "unidades de respuesta"
    return f"{unidades_str} asegura la escena antes de enviar {destino}."


_NOMBRES_INCIDENTE: dict[Incident, str] = {
    Incident.ATRAPADOS: "Personas atrapadas",
    Incident.COLAPSO: "Colapso estructural",
    Incident.INCENDIO: "Incendio activo",
    Incident.FUGA_GAS: "Fuga de gas / materiales peligrosos",
    Incident.HERIDO: "Atención de heridos",
    Incident.VIA_BLOQUEADA: "Vía bloqueada",
    Incident.OTRO: "Incidente reportado",
}

_TEXTO_PERSONAS: dict[People, str] = {
    People.UNA: "1 persona afectada",
    People.DE_2_A_5: "2 a 5 personas afectadas",
    People.DE_6_A_20: "6 a 20 personas afectadas",
    People.MAS_DE_20: "más de 20 personas afectadas",
    People.NO_SE: "cantidad de personas desconocida",
}


def fichas_pendientes(store: EventStore) -> list[Event]:
    """Devuelve las fichas que aún no tienen un evento `decision` que las referencie en refs[0].

    Función pura que reconstruye el estado directamente leyendo del store.
    """
    events = store.since({})
    fichas: dict[EventId, Event] = {}
    fichas_decididas: set[EventId] = set()

    for e in events:
        if e.type == "ficha":
            fichas[e.id] = e
        elif e.type == "decision" and e.refs:
            # Una decisión referencia la ficha en refs[0]
            fichas_decididas.add(e.refs[0])

    return [f for f_id, f in fichas.items() if f_id not in fichas_decididas]


def _seleccionar_tipo_incidente(reportes: Sequence[ReporteNormalizado]) -> Incident:
    """Selecciona el tipo principal de un grupo.

    Regla: prioridad por severidad y vida humana (menor valor numérico de Incident).
    ATRAPADOS > COLAPSO > INCENDIO > FUGA_GAS > HERIDO > VIA_BLOQUEADA > OTRO.
    """
    if not reportes:
        return Incident.OTRO
    return min(r.incident for r in reportes)


def _generar_mensaje_plantilla(
    tipo: Incident,
    reportes: Sequence[ReporteNormalizado],
    confianza: float,
    responsable: str | None,
    unidades: int | None,
    recomendacion_riesgo: str | None = None,
) -> str:
    """Redacta el mensaje operativo usando la plantilla oficial determinista."""
    rep_primero = reportes[0]
    loc = rep_primero.location

    # Ubicación legible
    if loc.label:
        ubi = f"en {loc.label}"
    elif loc.geohash:
        ubi = f"en geohash {loc.geohash}"
    elif loc.barrio is not None:
        ubi = f"en barrio {loc.barrio}"
    else:
        ubi = "en ubicación sin registrar"

    incertidumbre = f"aprox. {loc.radius_m} m"
    tipo_desc = _NOMBRES_INCIDENTE.get(tipo, "Emergencia")
    personas_desc = _TEXTO_PERSONAS.get(rep_primero.people, "personas afectadas")
    conf_pct = int(round(confianza * 100))

    msg = (
        f"{tipo_desc} {ubi} (incertidumbre {incertidumbre}), {personas_desc}. "
        f"Confianza: {conf_pct}%."
    )

    if recomendacion_riesgo:
        msg += f" {recomendacion_riesgo.strip()}"

    # Advertencias de recursos y responsable
    if responsable is None:
        msg += " Sin responsable identificado en la matriz EMRE; escalar al coordinador."
    elif unidades is not None and unidades == 0:
        msg += (
            f" ADVERTENCIA: La entidad {responsable} no tiene unidades disponibles"
            " (0 libres); se sugiere escalar al coordinador."
        )

    return msg


@dataclass(frozen=True, slots=True)
class CasoCoordinacion:
    """Todo lo determinista de una ficha, listo para redactar el mensaje (con o sin LLM).

    Se arma en el hilo del nodo leyendo el registro; la redacción con LLM puede correr en
    otro hilo; `publicar` vuelve a revisar que la ficha siga haciendo falta.
    """

    incidente_event: Event
    incident_id: str
    tipo: Incident
    miembros: tuple[ReporteNormalizado, ...]
    confianza: float
    rumor: bool
    responsable: str | None
    unidades: int | None
    risk_warning: str | None
    recomendacion_riesgo: str | None
    unidades_riesgo: tuple[str, ...]
    es_replanificacion: bool
    citas: tuple[Citation, ...]
    pasajes: tuple[Passage, ...]  # RAG: texto del protocolo para la redacción
    fichas_previas: int  # cuántas fichas tenía el incidente al preparar


# Redacción con LLM: lo que la unidad necesita oír por radio, sin inventar.
SYSTEM_REDACCION = """Eres el redactor de despachos del sistema Guadua (Manizales). Escribes \
el mensaje que el operador transmitirá por radio a la unidad asignada.
Reglas:
- Español, frases cortas e imperativas, máximo 4 frases y 450 caracteres.
- Usa SOLO los datos del caso y los textos de los ciudadanos. No inventes direcciones, \
cantidades, víctimas ni recursos.
- Incluye: qué pasa y dónde (con la incertidumbre de la ubicación), cuántas personas si se \
sabe, la advertencia de riesgo para la unidad si la hay y una o dos acciones del protocolo \
aplicable que de verdad correspondan al caso.
- Si la confianza es baja o es rumor, dilo ("verificar al llegar").
- No cambies la entidad asignada ni prometas unidades que no estén.
Responde SOLO con {"final": {"message": "<texto>"}}."""

MAX_MENSAJE = 600
MAX_TEXTO_CIUDADANO = 220
MAX_PASAJE = 450


class AgenteCoordinacion:
    """Agente de coordinación que genera fichas a partir de incidentes agrupados.

    La asignación de responsables es 100% determinista según la matriz EMRE. El LLM
    redacta el mensaje de despacho con los textos de los ciudadanos y el protocolo que
    recupera el RAG; si no hay LLM o falla, queda la plantilla (`from_template=True`).
    """

    def __init__(
        self,
        node_id: str,
        next_seq: Callable[[], int],
        llm: LLMProvider | None = None,
        retriever: Retriever | None = None,
    ) -> None:
        self.node_id = node_id
        self.next_seq = next_seq
        self.llm = llm
        self.retriever = retriever

    # --- API de siempre -------------------------------------------------------------------

    def procesar_incidente(self, incidente_event: Event, store: EventStore) -> Event | None:
        """Genera una ficha para un incidente agrupado (todo en línea).

        Si ya existe una ficha pendiente o aprobada para este incidente, no duplica.
        Si la última ficha fue rechazada, replanea generando una nueva ficha.
        """
        caso = self.preparar(incidente_event, store)
        if caso is None:
            return None
        mensaje, from_template = self.redactar(caso)
        return self.publicar(caso, mensaje, from_template, store)

    def procesar_decision(self, decision_event: Event, store: EventStore) -> Event | None:
        """Procesa un evento decision; si fue rechazar, replanea y publica una nueva ficha."""
        inc_ev = self.incidente_a_replanear(decision_event, store)
        return self.procesar_incidente(inc_ev, store) if inc_ev is not None else None

    # --- Pasos ----------------------------------------------------------------------------

    def preparar(self, incidente_event: Event, store: EventStore) -> CasoCoordinacion | None:
        """Lo determinista: responsable EMRE, unidades, riesgos, citas y pasajes del RAG.
        None si el incidente ya tiene una ficha vigente (pendiente o aprobada)."""
        if incidente_event.type != "incidente_agrupado":
            raise ValueError(
                f"Se esperaba evento incidente_agrupado, se recibió: {incidente_event.type!r}"
            )

        inc_payload = IncidenteAgrupado.model_validate(incidente_event.payload)
        incident_id = inc_payload.incident_id

        # 1. Reconstruir estado actual de reportes y fichas del store
        reportes_by_id: dict[str, ReporteNormalizado] = {}
        fichas_del_incidente: list[tuple[Event, Ficha]] = []
        decisiones_by_ficha_id: dict[EventId, Decision] = {}
        for e in store.since({}):
            if e.type == "reporte_normalizado":
                r_p = ReporteNormalizado.model_validate(e.payload)
                reportes_by_id[r_p.report_id] = r_p
            elif e.type == "ficha":
                f_p = Ficha.model_validate(e.payload)
                if f_p.incident_id == incident_id:
                    fichas_del_incidente.append((e, f_p))
            elif e.type == "decision" and e.refs:
                decisiones_by_ficha_id[e.refs[0]] = Decision.model_validate(e.payload)

        fichas_del_incidente.sort(key=lambda item: (item[0].received_at, item[0].seq))

        # 2. Idempotencia y control de ciclo de vida
        entidades_rechazadas: set[str] = set()
        if fichas_del_incidente:
            ultima_ficha_ev, _ = fichas_del_incidente[-1]
            decision = decisiones_by_ficha_id.get(ultima_ficha_ev.id)
            if decision is None or decision.action in ("aprobar", "editar"):
                return None  # ficha pendiente, o incidente ya gestionado
            for f_ev, f_p in fichas_del_incidente:
                dec = decisiones_by_ficha_id.get(f_ev.id)
                if dec is not None and dec.action == "rechazar" and f_p.responsible:
                    entidades_rechazadas.add(f_p.responsible)

        # 3. Reportes miembros
        miembros = tuple(
            reportes_by_id[r_id] for r_id in inc_payload.report_ids if r_id in reportes_by_id
        )
        if not miembros:
            return None

        # Señales de riesgo de los miembros (D3b)
        senales_riesgo: list[str] = []
        for r in miembros:
            for s in r.risk_signals:
                if s not in senales_riesgo:
                    senales_riesgo.append(s)

        catalogo_riesgos = cargar_riesgos_unidad()
        advertencias: list[str] = []
        unidades_riesgo: list[str] = []
        for s in senales_riesgo:
            info = catalogo_riesgos.get(s)
            if info:
                advertencias.append(info.advertencia)
                if info.unidad_recomendada and info.unidad_recomendada not in unidades_riesgo:
                    unidades_riesgo.append(info.unidad_recomendada)
            else:
                advertencias.append(f"Riesgo en escena: {s}")
        risk_warning = "; ".join(advertencias) if advertencias else None

        # 4. Tipo principal y responsable determinista según la EMRE
        tipo_principal = _seleccionar_tipo_incidente(miembros)
        responsable = responsable_por_tipo(tipo_principal, excluidos=entidades_rechazadas)

        # 5. Disponibilidad de unidades
        unidades = recursos_disponibles(responsable) if responsable else None
        recomendacion_riesgo = (
            _construir_recomendacion_riesgo(unidades_riesgo, responsable, tipo_principal)
            if unidades_riesgo
            else None
        )

        # 6. Citas y pasajes del protocolo
        pasajes = self._buscar_pasajes(tipo_principal, miembros, senales_riesgo)
        citas = self._citas(tipo_principal, responsable, pasajes)

        return CasoCoordinacion(
            incidente_event=incidente_event,
            incident_id=incident_id,
            tipo=tipo_principal,
            miembros=miembros,
            confianza=inc_payload.confidence,
            rumor=inc_payload.rumor,
            responsable=responsable,
            unidades=unidades,
            risk_warning=risk_warning,
            recomendacion_riesgo=recomendacion_riesgo,
            unidades_riesgo=tuple(unidades_riesgo),
            es_replanificacion=bool(entidades_rechazadas),
            citas=citas,
            pasajes=pasajes,
            fichas_previas=len(fichas_del_incidente),
        )

    @staticmethod
    def redactar_plantilla(caso: CasoCoordinacion) -> tuple[str, bool]:
        """El mensaje determinista (sin LLM)."""
        plantilla = _generar_mensaje_plantilla(
            caso.tipo,
            caso.miembros,
            caso.confianza,
            caso.responsable,
            caso.unidades,
            recomendacion_riesgo=caso.recomendacion_riesgo,
        )
        if caso.es_replanificacion:
            plantilla += " Replanificación por rechazo previo de ficha."
        return plantilla, True

    def redactar(self, caso: CasoCoordinacion) -> tuple[str, bool]:
        """Mensaje de despacho: (texto, from_template). Puede correr en otro hilo: no toca
        el registro. Sin LLM, o si responde algo que no sirve, la plantilla."""
        plantilla, _ = self.redactar_plantilla(caso)
        if self.llm is None:
            return plantilla, True
        try:
            reply = self.llm.complete(
                [
                    Message("system", SYSTEM_REDACCION),
                    Message("user", _prompt_redaccion(caso)),
                ],
                [],
            )
        except Exception as exc:  # LLMUnavailable, red, cuota…: queda la plantilla
            logger.warning("%s: sin redacción del LLM (%s)", caso.incident_id, exc)
            return plantilla, True
        final = reply.final if isinstance(reply.final, dict) else {}
        mensaje = " ".join(str(final.get("message") or "").split())
        if len(mensaje) < 20:
            logger.warning("%s: el LLM no devolvió un mensaje usable", caso.incident_id)
            return plantilla, True
        return _con_guardias(mensaje, caso), False

    def publicar(
        self, caso: CasoCoordinacion, mensaje: str, from_template: bool, store: EventStore
    ) -> Event | None:
        """Guarda la ficha. Si mientras se redactaba apareció otra ficha del incidente
        (otro nodo, o un reintento), no duplica."""
        actuales = sum(
            1
            for e in store.since({})
            if e.type == "ficha" and e.payload.get("incident_id") == caso.incident_id
        )
        if actuales != caso.fichas_previas:
            return None
        ficha_payload = Ficha(
            incident_id=caso.incident_id,
            responsible=caso.responsable,
            units_available=caso.unidades,
            risk_warning=caso.risk_warning,
            message=mensaje,
            citations=caso.citas,
            from_template=from_template,
        )
        evento_ficha = Event(
            origin_node=self.node_id,
            seq=self.next_seq(),
            category=EventCategory.DERIVADO,
            type="ficha",
            payload=ficha_payload.model_dump(),
            refs=(caso.incidente_event.id,),
            received_at=caso.incidente_event.received_at,
        )
        validate_event(evento_ficha)
        store.append(evento_ficha)
        return evento_ficha

    def incidente_a_replanear(self, decision_event: Event, store: EventStore) -> Event | None:
        """El incidente_agrupado de una decisión `rechazar`, o None."""
        if decision_event.type != "decision":
            raise ValueError(f"Se esperaba evento decision, se recibió: {decision_event.type!r}")
        dec_payload = Decision.model_validate(decision_event.payload)
        if dec_payload.action != "rechazar":
            return None
        ficha_ev = store.get(decision_event.refs[0])
        if ficha_ev is None:
            return None
        ficha_payload = Ficha.model_validate(ficha_ev.payload)
        inc_ev = store.get(ficha_ev.refs[0]) if ficha_ev.refs else None
        if inc_ev is None or inc_ev.type != "incidente_agrupado":
            for e in reversed(store.since({})):
                if (
                    e.type == "incidente_agrupado"
                    and e.payload.get("incident_id") == ficha_payload.incident_id
                ):
                    return e
            return None
        return inc_ev

    # --- RAG ------------------------------------------------------------------------------

    def _buscar_pasajes(
        self,
        tipo: Incident,
        miembros: Sequence[ReporteNormalizado],
        senales_riesgo: Sequence[str],
    ) -> tuple[Passage, ...]:
        """Pasajes del protocolo para el caso: la consulta lleva el tipo, las señales de
        riesgo y lo que escribieron los ciudadanos (no solo el barrio)."""
        if self.retriever is None:
            return ()
        textos = " ".join((r.text or "")[:MAX_TEXTO_CIUDADANO] for r in miembros[:3])
        query = " ".join(
            [_CONSULTA_POR_TIPO.get(tipo, ""), " ".join(senales_riesgo).replace("_", " "), textos]
        )
        try:
            encontrados = self.retriever.search(query, k=8)
        except Exception:
            logger.exception("falló el RAG de la coordinación")
            return ()
        # El mejor pasaje oficial (la cita verificable) y el mejor resumen del equipo (lo
        # táctico, marcado como ejemplo). Si solo hay de un tipo, los dos primeros.
        oficial = next((p for p in encontrados if not p.is_example), None)
        ejemplo = next((p for p in encontrados if p.is_example), None)
        elegidos = tuple(p for p in (oficial, ejemplo) if p is not None)
        return elegidos if len(elegidos) == 2 else tuple(encontrados[:2])

    @staticmethod
    def _citas(
        tipo: Incident, responsable: str | None, pasajes: Sequence[Passage]
    ) -> tuple[Citation, ...]:
        """La cita de la matriz EMRE (quién responde) y las de los pasajes del RAG."""
        citas: list[Citation] = []
        cita_emre = cita_por_tipo(tipo, responsable)
        if cita_emre:
            citas.append(cita_emre)
        for p in pasajes:
            citas.append(
                Citation(
                    document=p.document,
                    section=p.section,
                    version=p.version,
                    is_example=p.is_example,
                )
            )
        return tuple(citas)


# Consulta al RAG por tipo: el vocabulario de los protocolos, no el del ciudadano.
_CONSULTA_POR_TIPO: dict[Incident, str] = {
    Incident.ATRAPADOS: "búsqueda y rescate de personas atrapadas en estructuras colapsadas",
    Incident.COLAPSO: "colapso de edificación evaluación estructural búsqueda y rescate",
    Incident.INCENDIO: "incendio estructural extinción evacuación",
    Incident.FUGA_GAS: "fuga de gas suspensión del servicio fuentes de ignición evacuación",
    Incident.HERIDO: "atención prehospitalaria heridos traslado a la red de urgencias",
    Incident.VIA_BLOQUEADA: "vía bloqueada remoción de escombros desvío de tránsito",
    Incident.OTRO: "atención de emergencias",
}


def _prompt_redaccion(caso: CasoCoordinacion) -> str:
    primero = caso.miembros[0]
    loc = primero.location
    lugar = loc.label or (f"geohash {loc.geohash}" if loc.geohash else "ubicación sin registrar")
    datos = {
        "tipo": _NOMBRES_INCIDENTE.get(caso.tipo, "Emergencia"),
        "lugar": lugar,
        "incertidumbre_m": loc.radius_m,
        "personas": _TEXTO_PERSONAS.get(primero.people, "desconocido"),
        "senales_vitales": sorted({s for r in caso.miembros for s in r.vital_signs}),
        "reportes": len(caso.miembros),
        "textos_de_los_ciudadanos": [
            (r.text or "")[:MAX_TEXTO_CIUDADANO] for r in caso.miembros[:3] if r.text
        ],
        "lo_vio_quien_reporta": any(r.direct_observation for r in caso.miembros),
        "confianza": f"{round(caso.confianza * 100)} %",
        "rumor": caso.rumor,
        "entidad_asignada": caso.responsable or "sin responsable: escalar al coordinador",
        "unidades_libres": caso.unidades,
        "riesgo_para_la_unidad": caso.risk_warning,
        "recomendacion_de_seguridad": caso.recomendacion_riesgo,
        "replanificacion": caso.es_replanificacion,
        "protocolo_aplicable": [
            f"{p.document} {p.section}: {p.text[:MAX_PASAJE]}" for p in caso.pasajes
        ],
    }
    return "Caso:\n" + json.dumps(datos, ensure_ascii=False)


def _con_guardias(mensaje: str, caso: CasoCoordinacion) -> str:
    """Lo que el LLM no puede omitir: el lugar, la falta de unidades y la unidad
    especializada ante riesgos. Se agrega al final si no lo dijo."""
    lugar = caso.miembros[0].location.label
    if lugar and lugar.lower() not in mensaje.lower():
        mensaje = f"{lugar}: {mensaje}"
    if caso.unidades == 0 and "unidad" not in mensaje.lower():
        mensaje += (
            f" ADVERTENCIA: {caso.responsable} no tiene unidades disponibles;"
            " escalar al coordinador."
        )
    if caso.responsable is None and "coordinador" not in mensaje.lower():
        mensaje += " Sin responsable en la matriz EMRE: escalar al coordinador."
    if (
        caso.recomendacion_riesgo
        and caso.unidades_riesgo
        and not any(u.lower() in mensaje.lower() for u in caso.unidades_riesgo)
    ):
        mensaje += f" {caso.recomendacion_riesgo.strip()}"
    if caso.es_replanificacion and "replan" not in mensaje.lower():
        mensaje += " (Replanificación por rechazo previo.)"
    if len(mensaje) > MAX_MENSAJE:
        mensaje = mensaje[: MAX_MENSAJE - 1].rsplit(" ", 1)[0] + "…"
    return mensaje


def coordinar_incidente(
    incidente_event: Event,
    store: EventStore,
    node_id: str,
    next_seq: Callable[[], int],
    llm: LLMProvider | None = None,
    retriever: Retriever | None = None,
) -> Event | None:
    """Función de conveniencia para coordinar un incidente con el agente."""
    agente = AgenteCoordinacion(node_id, next_seq, llm=llm, retriever=retriever)
    return agente.procesar_incidente(incidente_event, store)
