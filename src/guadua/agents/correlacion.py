"""Agente 3: Correlación de reportes de emergencia.

Compara cada reporte normalizado entrante contra el CENTRO de los grupos abiertos
para evitar el efecto cadena (A~B, B~C, A~C).
CONTRATO: genera y valida eventos `incidente_agrupado` (docs/contratos.md §Catálogo).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable
from difflib import SequenceMatcher
from enum import Enum
from typing import Literal

from guadua.protocol.header import GEOHASH_ALPHABET, Incident
from guadua.store.event_types import (
    IncidenteAgrupado,
    ReporteNormalizado,
    validate_event,
)
from guadua.store.events import Event, EventCategory, EventStore

# --- Constantes y umbrales con nombre ---------------------------------------------------

# Ventana de tiempo máxima entre reportes consecutivos para considerar un grupo abierto
TIME_WINDOW_SECONDS: float = 7200.0  # 2 horas

# Antigüedad máxima total de un grupo desde su primer reporte recibido
MAX_GROUP_AGE_SECONDS: float = 21600.0  # 6 horas

# Margen sobre la suma de radios para considerar un caso "dudoso" (espacial geohash)
MARGIN_DUDOSO_METERS: float = 150.0

# Tope máximo para la banda de duda espacial
MAX_DISTANCE_DUDOSO_METERS: float = 600.0

# Umbrales para afinar según similitud de texto (SequenceMatcher)
TEXT_SIMILARITY_MIN_CONFIRM: float = 0.5
TEXT_SIMILARITY_DEGRADE_THRESHOLD: float = 0.2

# Confianza provisional (D4 lo reemplaza con la fórmula de §12)
CONFIDENCE_SEGURO: float = 0.8
CONFIDENCE_DUDOSO: float = 0.4
CONFIDENCE_NUEVO: float = 0.6


# --- Matriz de compatibilidad de tipos (Incident) --------------------------------------


class Compatibilidad(Enum):
    ALTA = "alta"
    MEDIA = "media"
    INCOMPATIBLE = "incompatible"


_PARES_ALTA: set[frozenset[Incident]] = {
    frozenset({Incident.COLAPSO, Incident.ATRAPADOS}),
    frozenset({Incident.COLAPSO, Incident.HERIDO}),
    frozenset({Incident.ATRAPADOS, Incident.HERIDO}),
    frozenset({Incident.INCENDIO, Incident.FUGA_GAS}),
}

_PARES_MEDIA: set[frozenset[Incident]] = {
    frozenset({Incident.COLAPSO, Incident.INCENDIO}),
    frozenset({Incident.COLAPSO, Incident.FUGA_GAS}),
    frozenset({Incident.INCENDIO, Incident.ATRAPADOS}),
    frozenset({Incident.INCENDIO, Incident.HERIDO}),
    frozenset({Incident.FUGA_GAS, Incident.ATRAPADOS}),
    frozenset({Incident.FUGA_GAS, Incident.HERIDO}),
    frozenset({Incident.COLAPSO, Incident.VIA_BLOQUEADA}),
}


def obtener_compatibilidad(t1: Incident, t2: Incident) -> Compatibilidad:
    """Evalúa la compatibilidad entre dos tipos de incidentes."""
    if t1 == Incident.OTRO and t2 == Incident.OTRO:
        return Compatibilidad.MEDIA
    if t1 == t2:
        return Compatibilidad.ALTA
    par = frozenset({t1, t2})
    if par in _PARES_ALTA:
        return Compatibilidad.ALTA
    if par in _PARES_MEDIA:
        return Compatibilidad.MEDIA
    return Compatibilidad.INCOMPATIBLE


def compatibilidad_con_grupo(
    incident: Incident, miembros: list[ReporteNormalizado]
) -> Compatibilidad:
    """Un reporte es compatible con un grupo solo si lo es con TODOS los tipos de sus miembros."""
    if not miembros:
        return Compatibilidad.INCOMPATIBLE
    niveles = [obtener_compatibilidad(incident, m.incident) for m in miembros]
    if any(n == Compatibilidad.INCOMPATIBLE for n in niveles):
        return Compatibilidad.INCOMPATIBLE
    if any(n == Compatibilidad.MEDIA for n in niveles):
        return Compatibilidad.MEDIA
    return Compatibilidad.ALTA


# --- Decodificación geohash y distancia Haversine --------------------------------------


def decode_geohash(geohash: str) -> tuple[float, float]:
    """Decodifica un geohash a (latitud, longitud)."""
    lat_interval = [-90.0, 90.0]
    lon_interval = [-180.0, 180.0]
    is_even = True
    for char in geohash.lower():
        val = GEOHASH_ALPHABET.find(char)
        if val == -1:
            raise ValueError(f"Carácter de geohash inválido: {char!r}")
        for mask in (16, 8, 4, 2, 1):
            bit = 1 if (val & mask) else 0
            if is_even:
                mid = (lon_interval[0] + lon_interval[1]) / 2.0
                if bit == 1:
                    lon_interval[0] = mid
                else:
                    lon_interval[1] = mid
            else:
                mid = (lat_interval[0] + lat_interval[1]) / 2.0
                if bit == 1:
                    lat_interval[0] = mid
                else:
                    lat_interval[1] = mid
            is_even = not is_even
    return (lat_interval[0] + lat_interval[1]) / 2.0, (lon_interval[0] + lon_interval[1]) / 2.0


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcula la distancia ortodrómica en metros entre dos coordenadas."""
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


def calcular_similitud_texto(text1: str | None, text2: str | None) -> float | None:
    """Calcula similitud de texto con SequenceMatcher.

    Devuelve None si alguno es None o vacío (similitud neutra).
    """
    if not text1 or not text2:
        return None
    s1 = " ".join(text1.lower().split())
    s2 = " ".join(text2.lower().split())
    if not s1 or not s2:
        return None
    return SequenceMatcher(None, s1, s2).ratio()


# --- Representación del Centro de un Grupo ----------------------------------------------


class CentroGrupo:
    """Representa el centro de un grupo abierto reconstruido a partir de sus reportes."""

    def __init__(
        self,
        incident_id: str,
        report_ids: tuple[str, ...],
        link: Literal["seguro", "dudoso", "nuevo"],
        miembros: list[tuple[ReporteNormalizado, float]],  # (reporte, received_at)
    ) -> None:
        self.incident_id = incident_id
        self.report_ids = report_ids
        self.link = link
        self.miembros = miembros

        self.first_received_at = min(r_at for _, r_at in miembros)
        self.last_received_at = max(r_at for _, r_at in miembros)

        # Ubicación: centroide geográfico o barrio
        self.has_geohash = all(m.location.geohash is not None for m, _ in miembros)
        self.has_barrio = all(m.location.barrio is not None for m, _ in miembros)
        self.barrio = miembros[0][0].location.barrio if self.has_barrio else None

        if self.has_geohash:
            coords = [decode_geohash(m.location.geohash) for m, _ in miembros]  # type: ignore[arg-type]
            self.lat = sum(c[0] for c in coords) / len(coords)
            self.lon = sum(c[1] for c in coords) / len(coords)
        else:
            self.lat = None
            self.lon = None

        # Rcentro = máximo radius_m de los miembros (NO la dispersión)
        self.radius_m = max(m.location.radius_m for m, _ in miembros)


# --- Correlacionador y lógica de agrupación ---------------------------------------------


class Correlacionador:
    """Agente de correlación que evalúa reportes contra el centro de los grupos abiertos.

    Reconstruye el estado de los incidentes leyendo directamente del almacén (EventStore),
    garantizando idempotencia y determinismo sin depender de memoria local persistente.
    """

    def __init__(
        self,
        node_id: str,
        next_seq: Callable[[], int],
    ) -> None:
        self.node_id = node_id
        self.next_seq = next_seq

    def procesar_reporte_normalizado(
        self,
        event: Event,
        store: EventStore,
    ) -> Event | None:
        """Procesa un evento de reporte_normalizado y publica incidente_agrupado en el store."""
        if event.type != "reporte_normalizado":
            raise ValueError(f"Se esperaba evento reporte_normalizado, se recibió: {event.type!r}")

        norm_report = ReporteNormalizado.model_validate(event.payload)

        # 1. Reconstruir estado actual de reportes e incidentes desde store.since({})
        existing_events = store.since({})

        reports_by_id: dict[str, tuple[ReporteNormalizado, float]] = {}
        incidents_events: dict[str, list[tuple[Event, IncidenteAgrupado]]] = defaultdict(list)

        for e in existing_events:
            if e.type == "reporte_normalizado":
                r_payload = ReporteNormalizado.model_validate(e.payload)
                reports_by_id[r_payload.report_id] = (r_payload, e.received_at)
            elif e.type == "incidente_agrupado":
                inc_payload = IncidenteAgrupado.model_validate(e.payload)
                incidents_events[inc_payload.incident_id].append((e, inc_payload))

        # Registrar el reporte actual para la construcción de grupos
        reports_by_id[norm_report.report_id] = (norm_report, event.received_at)

        # Obtener el estado más reciente de cada incidente agrupado
        latest_incidents: dict[str, tuple[Event, IncidenteAgrupado]] = {}
        for inc_id, ev_list in incidents_events.items():
            ev_list.sort(key=lambda item: (item[0].received_at, item[0].seq))
            latest_incidents[inc_id] = ev_list[-1]

        # Idempotencia: si este report_id ya fue agrupado en algún incidente, no duplicar
        for _, (_, inc) in latest_incidents.items():
            if norm_report.report_id in inc.report_ids:
                # Ya procesado, no generar evento duplicado
                return None

        # 2. Filtrar grupos abiertos candidatos (link != "dudoso")
        grupos_abiertos: list[CentroGrupo] = []
        for inc_id, (_, inc) in latest_incidents.items():
            if inc.link not in ("nuevo", "seguro"):
                # "dudoso" queda como incidente propio pendiente de revisión humana
                continue
            miembros: list[tuple[ReporteNormalizado, float]] = []
            todos_presentes = True
            for r_id in inc.report_ids:
                if r_id in reports_by_id:
                    miembros.append(reports_by_id[r_id])
                else:
                    todos_presentes = False
                    break
            if todos_presentes and miembros:
                grupos_abiertos.append(CentroGrupo(inc_id, inc.report_ids, inc.link, miembros))

        # 3. Evaluar el reporte contra cada grupo abierto
        candidatos_seguro: list[tuple[float, CentroGrupo]] = []
        candidatos_dudoso: list[CentroGrupo] = []

        for grupo in grupos_abiertos:
            resultado, dist = self._evaluar_contra_grupo(norm_report, event.received_at, grupo)
            if resultado == "seguro":
                candidatos_seguro.append((dist, grupo))
            elif resultado == "dudoso":
                candidatos_dudoso.append(grupo)

        # 4. Decidir vínculo y grupo
        if candidatos_seguro:
            # Tomar el grupo seguro más cercano al centroide
            candidatos_seguro.sort(key=lambda x: x[0])
            mejor_grupo = candidatos_seguro[0][1]
            incident_id = mejor_grupo.incident_id
            report_ids = (*mejor_grupo.report_ids, norm_report.report_id)
            link: Literal["seguro", "dudoso", "nuevo"] = "seguro"
            confidence = CONFIDENCE_SEGURO
        elif candidatos_dudoso:
            # NOTA CONTRACTUAL (Regla 9): El contrato no especifica grupo de referencia
            # para vínculos dudosos y no se cambian contratos. El reporte dudoso queda
            # como incidente propio con link="dudoso" para revisión del operador.
            incident_id = norm_report.report_id
            report_ids = (norm_report.report_id,)
            link = "dudoso"
            confidence = CONFIDENCE_DUDOSO
        else:
            # Incidente nuevo
            incident_id = norm_report.report_id
            report_ids = (norm_report.report_id,)
            link = "nuevo"
            confidence = CONFIDENCE_NUEVO

        # 5. Construir y validar evento inmutable
        payload = IncidenteAgrupado(
            incident_id=incident_id,
            report_ids=report_ids,
            link=link,
            confidence=confidence,
            rumor=False,
            contradictions=(),
        )

        seq = self.next_seq()
        nuevo_evento = Event(
            origin_node=self.node_id,
            seq=seq,
            category=EventCategory.DERIVADO,
            type="incidente_agrupado",
            payload=payload.model_dump(),
            refs=(event.id,),
            received_at=event.received_at,
        )

        validate_event(nuevo_evento)
        store.append(nuevo_evento)
        return nuevo_evento

    def _evaluar_contra_grupo(
        self,
        reporte: ReporteNormalizado,
        received_at: float,
        grupo: CentroGrupo,
    ) -> tuple[Literal["seguro", "dudoso", "nuevo"], float]:
        """Compara un reporte normalizado contra el centro del grupo."""
        # 1. Ventana de tiempo (usa received_at, NUNCA device_time)
        delta_last = received_at - grupo.last_received_at
        delta_first = received_at - grupo.first_received_at
        if (
            delta_last > TIME_WINDOW_SECONDS
            or delta_first > MAX_GROUP_AGE_SECONDS
            or delta_last < 0
        ):
            return "nuevo", float("inf")

        # 2. Compatibilidad de tipos con TODOS los miembros del grupo
        miembros_payloads = [m for m, _ in grupo.miembros]
        comp = compatibilidad_con_grupo(reporte.incident, miembros_payloads)
        if comp == Compatibilidad.INCOMPATIBLE:
            return "nuevo", float("inf")

        # 3. Similitud de texto (afina, no es criterio duro; neutro si no hay texto)
        sim_textos = [
            calcular_similitud_texto(reporte.text, m.text)
            for m in miembros_payloads
            if m.text is not None
        ]
        sims_validas = [s for s in sim_textos if s is not None]
        sim_texto = max(sims_validas) if sims_validas else None

        # 4. Comparación espacial
        # Caso A: Barrio contra Geohash (o viceversa) -> siempre "dudoso"
        if (reporte.location.barrio is not None and grupo.has_geohash) or (
            reporte.location.geohash is not None and grupo.has_barrio
        ):
            return "dudoso", float("inf")

        # Caso B: Ambos por Barrio
        if reporte.location.barrio is not None and grupo.has_barrio:
            if reporte.location.barrio != grupo.barrio:
                return "nuevo", float("inf")

            # Mismo barrio: máximo "dudoso". Solo sube a "seguro" con compatibilidad ALTA
            # y similitud de texto >= TEXT_SIMILARITY_MIN_CONFIRM.
            if (
                comp == Compatibilidad.ALTA
                and sim_texto is not None
                and sim_texto >= TEXT_SIMILARITY_MIN_CONFIRM
            ):
                return "seguro", 0.0
            return "dudoso", 0.0

        # Caso C: Ambos por Geohash (contra el Centroide del grupo)
        if reporte.location.geohash is not None and grupo.lat is not None and grupo.lon is not None:
            lat_r, lon_r = decode_geohash(reporte.location.geohash)
            d = haversine_distance_m(grupo.lat, grupo.lon, lat_r, lon_r)
            r_total = grupo.radius_m + reporte.location.radius_m

            if d <= r_total:
                if comp == Compatibilidad.ALTA:
                    # Si el texto es extremadamente disímil, degrada a dudoso
                    if sim_texto is not None and sim_texto < TEXT_SIMILARITY_DEGRADE_THRESHOLD:
                        return "dudoso", d
                    return "seguro", d
                else:  # Compatibilidad MEDIA
                    # Si están muy cerca (misma cuadra) y texto confirma, sube a seguro
                    r_min = min(grupo.radius_m, reporte.location.radius_m)
                    if (
                        d <= r_min
                        and sim_texto is not None
                        and sim_texto >= TEXT_SIMILARITY_MIN_CONFIRM
                    ):
                        return "seguro", d
                    return "dudoso", d

            elif d <= r_total + MARGIN_DUDOSO_METERS and d <= MAX_DISTANCE_DUDOSO_METERS:
                return "dudoso", d
            else:
                return "nuevo", d

        return "nuevo", float("inf")


def correlacionar_reporte(
    event: Event,
    store: EventStore,
    node_id: str,
    next_seq: Callable[[], int],
) -> Event | None:
    """Función de conveniencia para procesar un reporte normalizado con el agente."""
    agente = Correlacionador(node_id, next_seq)
    return agente.procesar_reporte_normalizado(event, store)
