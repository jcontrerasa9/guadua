"""El nodo publica `urgencia_estimada` (N7) y las tarjetas toman su nivel solas."""

import asyncio

from guadua.classifier.urgency import Urgency
from guadua.node import service
from guadua.node.service import Node
from guadua.node.views import PROVISIONAL_STEP, incident_cards, incident_detail
from guadua.protocol.header import Flag, Header, Incident, MessageType, encode
from guadua.protocol.text import encode_text
from guadua.store.event_types import UrgenciaEstimada
from guadua.store.sqlite import SQLiteEventStore
from tests.test_classifier import BrokenClassifier
from tests.test_node_cluster import cluster, sync

NOW = 1_794_573_600.0


def message(incident: Incident, flags: Flag, text: str, seq: int = 7) -> bytes:
    header = Header(MessageType.REPORTE, 0xA1B2C3D4, seq, 3600, incident, 1, flags, barrio=0)
    return encode(header) + encode_text(text)


ARBOL = message(Incident.VIA_BLOQUEADA, Flag.OBSERVACION_DIRECTA, "se cayo un arbol y tapo la via")
ATRAPADOS = message(Incident.COLAPSO, Flag.ATRAPADOS, "colapso casa 3 atrapados", seq=8)


def node(**kwargs) -> Node:
    return Node("A", SQLiteEventStore(), clock=lambda: NOW, **kwargs)


def urgencies(n: Node) -> list[UrgenciaEstimada]:
    return [
        UrgenciaEstimada.model_validate(e.payload)
        for e in n.store.since({})
        if e.type == "urgencia_estimada"
    ]


def test_la_tarjeta_toma_el_nivel_del_triaje() -> None:
    a = node()
    a.receive_message(ARBOL)
    a.receive_message(ATRAPADOS)
    (low,) = [u for u in urgencies(a) if not u.rules]
    assert low.source == "clasificador" and low.level < Urgency.CRITICA
    cards = incident_cards(a.store.since({}))
    assert [c.level for c in cards] == [Urgency.CRITICA, low.level]  # crítico primero
    detail = incident_detail(a.store.since({}), "a1b2c3d4-7")
    assert PROVISIONAL_STEP not in detail.steps
    assert detail.steps[0].detail.startswith("clasificador: nivel")


def test_el_evento_apunta_al_reporte_normalizado() -> None:
    a = node()
    a.receive_message(ATRAPADOS)
    (event,) = [e for e in a.store.since({}) if e.type == "urgencia_estimada"]
    assert a.store.get(event.refs[0]).type == "reporte_normalizado"
    assert event.category == "derivado" and event.received_at == NOW


def test_solo_el_nodo_dueno_publica_la_urgencia() -> None:
    nodes = cluster()
    nodes["A"].receive_message(ATRAPADOS)
    asyncio.run(sync(nodes))
    for name in "BC":
        events = nodes[name].store.since({})
        assert [e.origin_node for e in events if e.type == "urgencia_estimada"] == ["A"]
        assert not [e for e in events if e.origin_node == name]
        assert incident_cards(events)[0].level == Urgency.CRITICA


def test_si_falla_el_clasificador_quedan_las_reglas_y_el_ack() -> None:
    a = node(classifier=BrokenClassifier())
    assert not a.receive_message(ATRAPADOS).duplicate
    (estimate,) = urgencies(a)
    assert (estimate.level, estimate.source) == (Urgency.CRITICA, "reglas")
    assert "incidente_agrupado" in {e.type for e in a.store.since({})}


def test_si_falla_el_triaje_el_nodo_sigue(monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("triaje dañado")

    monkeypatch.setattr(service, "urgency_event", broken)
    a = node()
    assert not a.receive_message(ATRAPADOS).duplicate
    types = {e.type for e in a.store.since({})}
    assert "urgencia_estimada" not in types
    assert {"incidente_agrupado", "ficha"} <= types  # la correlación y la ficha siguen
    assert incident_cards(a.store.since({}))[0].level == Urgency.CRITICA  # provisional


def test_sin_clasificador_solo_reglas_y_revision_humana() -> None:
    a = node(classifier=None)
    a.receive_message(ARBOL)
    (estimate,) = urgencies(a)
    assert (estimate.level, estimate.source, estimate.ambiguous) == (Urgency.BAJA, "reglas", True)
    detail = incident_detail(a.store.since({}), "a1b2c3d4-7")
    assert detail.steps[0].detail == "reglas: nivel baja; ambiguo, revisar"
