"""Agente 6: Coordinación de respuesta y asignación de recursos.

Consulta la matriz EMRE de Manizales para determinar la entidad responsable,
verifica la disponibilidad de recursos y redacta la ficha de coordinación.
La ficha queda pendiente hasta la aprobación humana (decisión del operador).
CONTRATO: genera y valida eventos `ficha` (docs/contratos.md §Catálogo).
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from guadua.knowledge.matriz_emre import cita_por_tipo, recursos_disponibles, responsable_por_tipo
from guadua.knowledge.retriever import Retriever
from guadua.protocol.header import Incident, People
from guadua.providers.llm import LLMProvider, LLMUnavailable, Message
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


class AgenteCoordinacion:
    """Agente de coordinación que genera fichas a partir de incidentes agrupados.

    La asignación de responsables es 100% determinista según la matriz EMRE.
    El LLM únicamente asiste en la redacción del mensaje si está disponible y habilitado.
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

    def procesar_incidente(
        self,
        incidente_event: Event,
        store: EventStore,
    ) -> Event | None:
        """Genera una ficha para un incidente agrupado.

        Si ya existe una ficha pendiente o aprobada para este incidente, no duplica.
        Si la última ficha fue rechazada, replanea generando una nueva ficha.
        """
        if incidente_event.type != "incidente_agrupado":
            raise ValueError(
                f"Se esperaba evento incidente_agrupado, se recibió: {incidente_event.type!r}"
            )

        inc_payload = IncidenteAgrupado.model_validate(incidente_event.payload)
        incident_id = inc_payload.incident_id

        # 1. Reconstruir estado actual de reportes y fichas del store
        events = store.since({})
        reportes_by_id: dict[str, ReporteNormalizado] = {}
        fichas_del_incidente: list[tuple[Event, Ficha]] = []
        decisiones_by_ficha_id: dict[EventId, Decision] = {}

        for e in events:
            if e.type == "reporte_normalizado":
                r_p = ReporteNormalizado.model_validate(e.payload)
                reportes_by_id[r_p.report_id] = r_p
            elif e.type == "ficha":
                f_p = Ficha.model_validate(e.payload)
                if f_p.incident_id == incident_id:
                    fichas_del_incidente.append((e, f_p))
            elif e.type == "decision" and e.refs:
                d_p = Decision.model_validate(e.payload)
                decisiones_by_ficha_id[e.refs[0]] = d_p

        # Ordenar fichas cronológicamente
        fichas_del_incidente.sort(key=lambda item: (item[0].received_at, item[0].seq))

        # 2. Idempotencia y control de ciclo de vida
        entidades_rechazadas: set[str] = set()
        if fichas_del_incidente:
            ultima_ficha_ev, ultima_ficha_p = fichas_del_incidente[-1]
            decision = decisiones_by_ficha_id.get(ultima_ficha_ev.id)

            if decision is None:
                # Ya existe una ficha pendiente para este incidente; no duplicar
                return None
            elif decision.action in ("aprobar", "editar"):
                # Incidente ya gestionado; no crear nueva ficha
                return None
            elif decision.action == "rechazar":
                # Recopilar entidades previamente rechazadas para replanear
                for f_ev, f_p in fichas_del_incidente:
                    dec = decisiones_by_ficha_id.get(f_ev.id)
                    if dec is not None and dec.action == "rechazar" and f_p.responsible:
                        entidades_rechazadas.add(f_p.responsible)

        # 3. Obtener reportes miembros
        miembros: list[ReporteNormalizado] = []
        for r_id in inc_payload.report_ids:
            if r_id in reportes_by_id:
                miembros.append(reportes_by_id[r_id])

        if not miembros:
            return None

        # Evaluación de señales de riesgo de los miembros (D3b)
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

        # 4. Tipo principal de incidente y responsable determinista según la EMRE
        tipo_principal = _seleccionar_tipo_incidente(miembros)
        responsable = responsable_por_tipo(tipo_principal, excluidos=entidades_rechazadas)

        # 5. Disponibilidad de unidades
        unidades = recursos_disponibles(responsable) if responsable else None

        recomendacion_riesgo = (
            _construir_recomendacion_riesgo(unidades_riesgo, responsable, tipo_principal)
            if unidades_riesgo
            else None
        )

        # 6. Redacción del mensaje (LLM opcional o plantilla)
        mensaje, from_template = self._redactar_mensaje(
            tipo_principal,
            miembros,
            inc_payload.confidence,
            responsable,
            unidades,
            recomendacion_riesgo=recomendacion_riesgo,
            risk_warning=risk_warning,
            unidades_riesgo=unidades_riesgo,
            es_replanificacion=bool(entidades_rechazadas),
        )

        # 7. Citas oficiales
        citas = self._obtener_citas(tipo_principal, responsable, miembros[0])

        # 8. Construir evento Ficha
        ficha_payload = Ficha(
            incident_id=incident_id,
            responsible=responsable,
            units_available=unidades,
            risk_warning=risk_warning,
            message=mensaje,
            citations=citas,
            from_template=from_template,
        )

        seq = self.next_seq()
        evento_ficha = Event(
            origin_node=self.node_id,
            seq=seq,
            category=EventCategory.DERIVADO,
            type="ficha",
            payload=ficha_payload.model_dump(),
            refs=(incidente_event.id,),
            received_at=incidente_event.received_at,
        )

        validate_event(evento_ficha)
        store.append(evento_ficha)
        return evento_ficha

    def procesar_decision(
        self,
        decision_event: Event,
        store: EventStore,
    ) -> Event | None:
        """Procesa un evento decision; si fue rechazar, replanea y publica una nueva ficha."""
        if decision_event.type != "decision":
            raise ValueError(f"Se esperaba evento decision, se recibió: {decision_event.type!r}")
        dec_payload = Decision.model_validate(decision_event.payload)
        if dec_payload.action != "rechazar":
            return None

        # Encontrar la ficha referenciada
        ficha_ev = store.get(decision_event.refs[0])
        if ficha_ev is None:
            return None

        ficha_payload = Ficha.model_validate(ficha_ev.payload)

        # Encontrar el incidente_agrupado asociado a través de refs de la ficha
        inc_ev = store.get(ficha_ev.refs[0]) if ficha_ev.refs else None
        if inc_ev is None or inc_ev.type != "incidente_agrupado":
            # Buscar el evento incidente_agrupado más reciente para ese incident_id
            for e in reversed(store.since({})):
                if (
                    e.type == "incidente_agrupado"
                    and e.payload.get("incident_id") == ficha_payload.incident_id
                ):
                    inc_ev = e
                    break

        if inc_ev is None:
            return None

        return self.procesar_incidente(inc_ev, store)

    def _redactar_mensaje(
        self,
        tipo: Incident,
        miembros: Sequence[ReporteNormalizado],
        confianza: float,
        responsable: str | None,
        unidades: int | None,
        recomendacion_riesgo: str | None = None,
        risk_warning: str | None = None,
        unidades_riesgo: Sequence[str] = (),
        es_replanificacion: bool = False,
    ) -> tuple[str, bool]:
        """Redacta el mensaje operativo. Si el LLM no está disponible o falla, usa la plantilla."""
        mensaje_base = _generar_mensaje_plantilla(
            tipo,
            miembros,
            confianza,
            responsable,
            unidades,
            recomendacion_riesgo=recomendacion_riesgo,
        )
        if es_replanificacion:
            mensaje_base += " Replanificación por rechazo previo de ficha."

        if self.llm is None:
            return mensaje_base, True

        # Intento de redacción con LLM
        try:
            prompt_usuario = (
                f"Redacta un mensaje conciso de coordinación para la ficha de emergencia.\n"
                f"Tipo: {_NOMBRES_INCIDENTE.get(tipo, 'Emergencia')}\n"
                f"Ubicación: {miembros[0].location.label or 'Manizales'}\n"
                f"Incertidumbre: aprox. {miembros[0].location.radius_m} m\n"
                f"Personas: {_TEXTO_PERSONAS.get(miembros[0].people, 'desconocido')}\n"
                f"Confianza: {int(confianza * 100)}%\n"
                f"Entidad asignada: {responsable or 'Sin responsable'}\n"
                f"Unidades disponibles: {unidades}\n"
            )
            if risk_warning:
                prompt_usuario += f"Advertencia de riesgo en la escena: {risk_warning}\n"
            if recomendacion_riesgo:
                prompt_usuario += f"Recomendación de seguridad: {recomendacion_riesgo}\n"

            reply = self.llm.complete(
                messages=[
                    Message(
                        role="system",
                        content="Eres el asistente de redacción de fichas del sistema Guadua.",
                    ),
                    Message(role="user", content=prompt_usuario),
                ],
                tools=[],
            )

            if reply.final and isinstance(reply.final, dict) and "message" in reply.final:
                msg_llm = str(reply.final["message"]).strip()
                # Garantizar que advertencia de recursos o sin responsable no se pierda
                if unidades == 0 and "unidades" not in msg_llm.lower():
                    msg_llm += (
                        f" ADVERTENCIA: {responsable} no tiene unidades disponibles;"
                        " escalar al coordinador."
                    )
                if (
                    recomendacion_riesgo
                    and unidades_riesgo
                    and not any(u.lower() in msg_llm.lower() for u in unidades_riesgo)
                ):
                    msg_llm += f" {recomendacion_riesgo.strip()}"
                return msg_llm, False

        except (LLMUnavailable, Exception):
            pass

        return mensaje_base, True

    def _obtener_citas(
        self,
        tipo: Incident,
        responsable: str | None,
        reporte: ReporteNormalizado,
    ) -> tuple[Citation, ...]:
        """Recupera las citas normativas de la EMRE y del Retriever si está disponible."""
        citas_list: list[Citation] = []

        # 1. Cita oficial de la matriz EMRE
        cita_emre = cita_por_tipo(tipo, responsable)
        if cita_emre:
            citas_list.append(cita_emre)

        # 2. Cita de Retriever (RAG) si está configurado
        if self.retriever is not None:
            try:
                query = f"{_NOMBRES_INCIDENTE.get(tipo, '')} {reporte.location.label or ''}"
                pasajes = self.retriever.search(query, k=1)
                if pasajes:
                    p = pasajes[0]
                    citas_list.append(
                        Citation(
                            document=p.document,
                            section=p.section,
                            version=p.version,
                            is_example=p.is_example,
                        )
                    )
            except Exception:
                pass

        return tuple(citas_list)


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
