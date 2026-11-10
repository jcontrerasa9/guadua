"""Módulo puro para la extracción y consulta de personas a salvo (D2b).

CONTRATO: Lee eventos `a_salvo` del store y construye la lista para `GET /api/people`.
Ver docs/contratos.md, docs/diseno.md §6.3 y src/guadua/node/api.py.

NOTA PARA JACOBO:
Para implementar la ruta `GET /api/people` en `src/guadua/node/app.py`, basta con:
    from guadua.agents.personas_a_salvo import armar_people_list

    @app.get("/api/people", response_model=PeopleList)
    def get_people():
        return armar_people_list(node.store)
"""

from __future__ import annotations

from collections.abc import Iterable

from guadua.node.api import PeopleList, Person
from guadua.node.pipeline import barrio_names
from guadua.store.events import Event, EventStore


def _extraer_eventos(store_or_events: EventStore | Iterable[Event]) -> Iterable[Event]:
    """Obtiene un iterable de eventos ya sea desde un EventStore o una colección."""
    if hasattr(store_or_events, "since"):
        return store_or_events.since({})  # type: ignore[union-attr]
    return store_or_events  # type: ignore[return-value]


def obtener_personas_a_salvo(
    store_or_events: EventStore | Iterable[Event],
) -> list[Person]:
    """Extrae las personas a salvo a partir de los eventos del almacén.

    Para cada evento `a_salvo`:
    - Extrae report_id, name, barrio y received_at.
    - Resuelve el nombre textual del barrio usando el catálogo `barrios.json`.
    - Si llegan múltiples eventos con el mismo report_id, prevalece el más reciente.
    - Devuelve la lista ordenada por received_at descendente (más recientes primero).
    """
    events = _extraer_eventos(store_or_events)
    catalogo_barrios = barrio_names()

    # Deduplicar por report_id: si hay duplicados, conservar el de mayor received_at
    personas_por_reporte: dict[str, Person] = {}

    for ev in events:
        if ev.type != "a_salvo":
            continue

        payload = ev.payload or {}
        report_id = payload.get("report_id")
        if not report_id:
            continue

        raw_name = payload.get("name")
        name = str(raw_name).strip() if raw_name is not None and str(raw_name).strip() else None

        barrio_code = payload.get("barrio")
        barrio_label: str | None = None
        if barrio_code is not None and isinstance(barrio_code, int):
            barrio_label = catalogo_barrios.get(barrio_code, f"Barrio #{barrio_code}")

        persona = Person(
            report_id=report_id,
            name=name,
            barrio_label=barrio_label,
            received_at=ev.received_at,
        )

        existente = personas_por_reporte.get(report_id)
        if existente is None or persona.received_at >= existente.received_at:
            personas_por_reporte[report_id] = persona

    # Ordenar por received_at descendente y luego por report_id
    return sorted(personas_por_reporte.values(), key=lambda p: (-p.received_at, p.report_id))


def armar_people_list(
    store_or_events: EventStore | Iterable[Event],
) -> PeopleList:
    """Construye el esquema PeopleList para responder en GET /api/people."""
    personas = obtener_personas_a_salvo(store_or_events)
    return PeopleList(people=tuple(personas))
