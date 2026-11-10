"""Lo que el nodo dueño hace con cada reporte: normalizar, estimar la urgencia, agrupar y
armar la ficha.

    reporte → reporte_normalizado (reglas simples) ─┬→ urgencia_estimada (N7)
                                                    │     └→ (si es ambigua) urgencia_estimada
                                                    │        del agente de triaje (N8)
                                                    └→ incidente_agrupado (N9) → ficha (N10)

Solo el nodo dueño (el que recibió el reporte) crea los eventos derivados; los demás los
reciben por gossip y no recalculan (docs/diseno.md §9). La urgencia sale de reglas +
clasificador (classifier/triage.py); el agente de triaje (N8, agents/triaje.py) la revisa en
la zona gris con los límites de la regla 1.

La normalización es la de "reglas simples mientras no exista el Agente 1"
(docs/contratos.md): sale de la cabecera; del texto solo lee las señales de riesgo para
rescatistas (D3a, classifier/risk.py). Se detectan aquí, una sola vez: la urgencia las copia
y la ficha y la tarjeta las leen de este evento.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from guadua.classifier.risk import detect_risk_signals
from guadua.classifier.triage import UrgencyClassifier, triage
from guadua.protocol.header import Flag, People
from guadua.store.event_types import Location, Reporte, ReporteNormalizado
from guadua.store.events import Event, EventCategory

BARRIOS_PATH = Path(__file__).resolve().parents[3] / "data" / "barrios.json"
GEOHASH_RADIUS_M = 150  # geohash de 7 caracteres ≈ 150 m
BARRIO_RADIUS_M = 600  # sin GPS: el barrio completo

VITAL_FLAGS = {
    Flag.ATRAPADOS: "atrapados",
    Flag.NO_RESPIRA_O_SANGRADO: "no_respira_o_sangrado",
    Flag.NINOS_O_MAYORES: "ninos_o_mayores",
}


@cache
def barrio_names() -> dict[int, str]:
    try:
        return {b["code"]: b["name"] for b in json.loads(BARRIOS_PATH.read_text("utf-8"))}
    except (OSError, ValueError, KeyError):
        return {}


def location_of(reporte: Reporte) -> Location:
    if reporte.geohash is not None:
        return Location(geohash=reporte.geohash, radius_m=GEOHASH_RADIUS_M)
    return Location(
        barrio=reporte.barrio, radius_m=BARRIO_RADIUS_M, label=barrio_names().get(reporte.barrio)
    )


def normalize(reporte: Reporte) -> ReporteNormalizado:
    flags = Flag(reporte.flags)
    unknown = ["people"] if reporte.people == People.NO_SE else []
    return ReporteNormalizado(
        report_id=reporte.report_id,
        incident=reporte.incident,
        people=reporte.people,
        location=location_of(reporte),
        direct_observation=Flag.OBSERVACION_DIRECTA in flags,
        vital_signs=tuple(name for flag, name in VITAL_FLAGS.items() if flag in flags),
        risk_signals=detect_risk_signals(reporte),
        text=reporte.text,
        unknown_fields=tuple(unknown),
    )


def normalized_event(reporte_event: Event, *, node_id: str, seq: int) -> Event:
    payload = normalize(Reporte.model_validate(reporte_event.payload))
    return Event(
        origin_node=node_id,
        seq=seq,
        category=EventCategory.DERIVADO,
        type="reporte_normalizado",
        payload=payload.model_dump(mode="json"),
        refs=(reporte_event.id,),
        received_at=reporte_event.received_at,
    )


def urgency_event(
    normalized_event: Event,
    reporte_event: Event,
    classifier: UrgencyClassifier | None,
    *,
    node_id: str,
    seq: int,
) -> Event:
    """`urgencia_estimada` del reporte; apunta en `refs` al `reporte_normalizado`."""
    normalized = ReporteNormalizado.model_validate(normalized_event.payload)
    payload = triage(
        Reporte.model_validate(reporte_event.payload), classifier, normalized.risk_signals
    )
    return Event(
        origin_node=node_id,
        seq=seq,
        category=EventCategory.DERIVADO,
        type="urgencia_estimada",
        payload=payload.model_dump(mode="json"),
        refs=(normalized_event.id,),
        received_at=normalized_event.received_at,
    )
