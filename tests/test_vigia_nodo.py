"""Vigía (D6) dentro del nodo: publica `solicitud_vigia`, una vez, sin tocar urgencias."""

import warnings

import pytest

from guadua.node.app import create_app
from guadua.node.config import ConfigError, NodeConfig
from guadua.node.service import Node
from guadua.node.views import incident_cards, incident_detail
from guadua.store.event_types import SolicitudVigia
from guadua.store.sqlite import SQLiteEventStore
from tests.test_node_api import message

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

NOW = 1_794_573_600.0


def _node(name: str = "A") -> Node:
    return Node(name, SQLiteEventStore(), clock=lambda: NOW, classifier=None, vigia_critico_s=300)


def _with_report() -> tuple[Node, str]:
    node = _node()
    TestClient(create_app(node, gossip_interval=None)).post("/api/messages", content=message())
    (card,) = incident_cards(node.store.since({}))
    return node, card.incident_id


def _urgencies(node: Node) -> list[dict]:
    return [e.payload for e in node.store.since({}) if e.type == "urgencia_estimada"]


def test_un_caso_reciente_no_genera_solicitudes() -> None:
    node, _ = _with_report()
    assert node.watch(now=NOW + 60) == []


def test_un_critico_olvidado_genera_alerta_una_sola_vez_y_no_cambia_la_urgencia() -> None:
    node, iid = _with_report()
    before = _urgencies(node)
    (card,) = incident_cards(node.store.since({}))
    published = node.watch(now=NOW + 400)
    actions = {SolicitudVigia.model_validate(e.payload).action for e in published}
    assert "alerta_olvido" in actions
    assert all(e.payload["incident_id"] == iid for e in published)
    assert all(e.payload["level"] == card.level for e in published)
    assert node.watch(now=NOW + 800) == []  # ya avisó: no repite en cada ronda
    assert _urgencies(node) == before  # regla 1: el vigía nunca toca la urgencia
    (after,) = incident_cards(node.store.since({}))
    assert after.level == card.level and after.status == card.status


def test_el_tablero_ve_la_solicitud_en_los_pasos() -> None:
    node, iid = _with_report()
    node.watch(now=NOW + 400)
    detail = incident_detail(node.store.since({}), iid)
    steps = [s.detail for s in detail.steps if s.agent == "vigia"]
    assert any(s.startswith("alerta de olvido en el tablero: ALERTA DE OLVIDO") for s in steps)


def test_solo_el_dueno_del_incidente_publica() -> None:
    node, _ = _with_report()
    other = _node("B")
    for event in node.store.since({}):
        other.store.append(event)
    assert other.watch(now=NOW + 400) == []


def test_apagado_el_vigia_no_hace_nada() -> None:
    node, _ = _with_report()
    node.set_power(False)
    assert node.watch(now=NOW + 400) == []


def test_umbrales_del_vigia_desde_el_entorno() -> None:
    config = NodeConfig.from_env({"SARD_VIGIA_CRITICO_S": "60", "SARD_VIGIA_INTERVAL": "5"})
    assert config.vigia_critico_s == 60 and config.vigia_interval == 5
    assert config.vigia_olvido_s == 900
    with pytest.raises(ConfigError):
        NodeConfig.from_env({"SARD_VIGIA_INTERVAL": "0"})
