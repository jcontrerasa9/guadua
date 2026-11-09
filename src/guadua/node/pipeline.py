"""Lo que el nodo dueño hace con cada reporte: normalizar, agrupar y armar la ficha.

    reporte → reporte_normalizado (reglas simples) → incidente_agrupado (N9) → ficha (N10)

Solo el nodo dueño (el que recibió el reporte) crea los eventos derivados; los demás los
reciben por gossip y no recalculan (docs/diseno.md §9). La urgencia es de N7/N8: mientras no
exista, el tablero muestra el incidente como crítico provisional (ver views.py).

La normalización es la de "reglas simples mientras no exista el Agente 1"
(docs/contratos.md): sale solo de la cabecera, sin interpretar el texto.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

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
RISK_FLAGS = {Flag.INCENDIO: "incendio", Flag.RIESGO_ESCENA: "gas_o_cables"}


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
        risk_signals=tuple(name for flag, name in RISK_FLAGS.items() if flag in flags),
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
