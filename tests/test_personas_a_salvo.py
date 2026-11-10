"""Pruebas unitarias para el registro de personas a salvo (D2b)."""

from __future__ import annotations

import json

from guadua.agents.personas_a_salvo import armar_people_list, obtener_personas_a_salvo
from guadua.node.api import PeopleList, Person
from guadua.store.events import Event, EventCategory
from tests.fakes import InMemoryEventStore


def _evento_a_salvo(
    seq: int,
    report_id: str,
    name: str | None,
    barrio: int | None,
    received_at: float,
    node: str = "A",
) -> Event:
    return Event(
        origin_node=node,
        seq=seq,
        category=EventCategory.HECHO,
        type="a_salvo",
        payload={
            "report_id": report_id,
            "name": name,
            "barrio": barrio,
        },
        received_at=received_at,
    )


def test_personas_a_salvo_con_y_sin_nombre():
    """Verifica que se procesen correctamente reportes a_salvo con nombre y anónimos."""
    events = [
        _evento_a_salvo(
            seq=1,
            report_id="01020304-1",
            name="Carlos Gómez",
            barrio=5,  # Palermo
            received_at=100.0,
        ),
        _evento_a_salvo(
            seq=2,
            report_id="01020304-2",
            name=None,
            barrio=1,  # El Cable
            received_at=200.0,
        ),
        _evento_a_salvo(
            seq=3,
            report_id="01020304-3",
            name="   ",  # nombre con solo espacios debe tratarse como None
            barrio=3,  # Chipre
            received_at=300.0,
        ),
    ]

    personas = obtener_personas_a_salvo(events)
    assert len(personas) == 3

    # Ordenados por received_at descendente
    p0, p1, p2 = personas
    assert p0.report_id == "01020304-3"
    assert p0.name is None
    assert p0.barrio_label == "Chipre"
    assert p0.received_at == 300.0

    assert p1.report_id == "01020304-2"
    assert p1.name is None
    assert p1.barrio_label == "El Cable"
    assert p1.received_at == 200.0

    assert p2.report_id == "01020304-1"
    assert p2.name == "Carlos Gómez"
    assert p2.barrio_label == "Palermo"
    assert p2.received_at == 100.0


def test_mapeo_barrios_conocidos_desconocidos_y_nulos():
    """Comprueba resolución de etiquetas de barrio."""
    events = [
        _evento_a_salvo(seq=1, report_id="11111111-1", name="Ana", barrio=5, received_at=10.0),
        _evento_a_salvo(seq=2, report_id="22222222-1", name="Beto", barrio=9999, received_at=20.0),
        _evento_a_salvo(seq=3, report_id="33333333-1", name="Clara", barrio=None, received_at=30.0),
    ]

    personas = obtener_personas_a_salvo(events)
    assert len(personas) == 3
    # Clara (30.0) -> barrio None
    assert personas[0].barrio_label is None
    # Beto (20.0) -> Barrio #9999
    assert personas[1].barrio_label == "Barrio #9999"
    # Ana (10.0) -> Palermo
    assert personas[2].barrio_label == "Palermo"


def test_filtra_otros_tipos_de_eventos():
    """Solo los eventos 'a_salvo' deben considerarse."""
    events = [
        Event(
            origin_node="A",
            seq=1,
            category=EventCategory.HECHO,
            type="reporte",
            payload={"report_id": "00000001-1"},
            received_at=50.0,
        ),
        Event(
            origin_node="A",
            seq=2,
            category=EventCategory.DERIVADO,
            type="urgencia_estimada",
            payload={"level": 4, "floor": 4},
            received_at=60.0,
        ),
        _evento_a_salvo(
            seq=3,
            report_id="00000002-1",
            name="David",
            barrio=1,
            received_at=70.0,
        ),
    ]

    personas = obtener_personas_a_salvo(events)
    assert len(personas) == 1
    assert personas[0].report_id == "00000002-1"
    assert personas[0].name == "David"


def test_deduplicacion_por_report_id_conserva_mas_reciente():
    """Si el mismo report_id se emite varias veces, vale el de mayor received_at."""
    events = [
        _evento_a_salvo(
            seq=1,
            report_id="aabbccdd-1",
            name="Esteban",
            barrio=1,
            received_at=100.0,
        ),
        _evento_a_salvo(
            seq=2,
            report_id="aabbccdd-1",
            name="Esteban Martínez",
            barrio=5,
            received_at=150.0,
        ),
    ]

    personas = obtener_personas_a_salvo(events)
    assert len(personas) == 1
    assert personas[0].name == "Esteban Martínez"
    assert personas[0].barrio_label == "Palermo"
    assert personas[0].received_at == 150.0


def test_armar_people_list_construye_schema_valido_y_serializable():
    """PeopleList cumple con el contrato Schema de api.py y fixture api_examples.json."""
    events = [
        _evento_a_salvo(
            seq=1,
            report_id="00000063-3",
            name="Ana",
            barrio=5,  # Palermo
            received_at=1794574000.0,
        )
    ]

    plist = armar_people_list(events)
    assert isinstance(plist, PeopleList)
    assert len(plist.people) == 1
    p = plist.people[0]
    assert isinstance(p, Person)
    assert p.report_id == "00000063-3"
    assert p.name == "Ana"
    assert p.barrio_label == "Palermo"
    assert p.received_at == 1794574000.0

    # Serialización JSON idéntica al formato esperado por el endpoint
    raw_dict = json.loads(plist.model_dump_json())
    assert raw_dict == {
        "people": [
            {
                "report_id": "00000063-3",
                "name": "Ana",
                "barrio_label": "Palermo",
                "received_at": 1794574000.0,
            }
        ]
    }


def test_reconstruccion_desde_in_memory_event_store():
    """Verifica que armar_people_list funcione pasando directamente un EventStore."""
    store = InMemoryEventStore()
    store.append(
        _evento_a_salvo(
            seq=1,
            report_id="01020304-1",
            name="Gloria",
            barrio=1,
            received_at=500.0,
        )
    )
    store.append(
        Event(
            origin_node="A",
            seq=2,
            category=EventCategory.HECHO,
            type="reporte",
            payload={"report_id": "99999999-1"},
            received_at=550.0,
        )
    )
    store.append(
        _evento_a_salvo(
            seq=3,
            report_id="01020304-2",
            name="Hernán",
            barrio=15,
            received_at=600.0,
        )
    )

    plist = armar_people_list(store)
    assert len(plist.people) == 2
    assert [p.name for p in plist.people] == ["Hernán", "Gloria"]
