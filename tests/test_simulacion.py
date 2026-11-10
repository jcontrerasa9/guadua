"""Barra de simulación real (D9) y RAG en el nodo (D8).

D9: ráfaga y CORS en el proxy de fallos, apagado simulado y digest en el nodo, y que el
apagado simulado dispare la toma de trabajo de N12 como una caída de verdad.
"""

import asyncio
import random
import warnings

import httpx

from guadua.channel.burst import burst_messages
from guadua.channel.profiles import IDEAL, LORA
from guadua.channel.proxy import FaultProxy
from guadua.node.app import create_app
from guadua.node.intake import parse_message
from guadua.node.service import Node
from guadua.services.failover import converged
from guadua.store.event_types import Ficha
from guadua.store.sqlite import SQLiteEventStore
from tests.test_node_api import check, message
from tests.test_resilience import Cluster, received_only, takeovers

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

NOW = 1_794_573_600.0


def _node(**kwargs) -> Node:
    return Node("A", SQLiteEventStore(), clock=lambda: NOW, classifier=None, **kwargs)


def _proxy(node: Node, profile=IDEAL) -> tuple[FaultProxy, httpx.AsyncClient]:
    app = create_app(node, gossip_interval=None)
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nodo")
    proxy = FaultProxy("http://nodo", profile, seed=3, time_scale=0, client=upstream)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://p")
    return proxy, client


def _reportes(node: Node) -> list:
    return [e for e in node.store.since({}) if e.type == "reporte"]


# --- Ráfaga ---------------------------------------------------------------------------------


def test_los_mensajes_de_la_rafaga_son_reportes_validos_y_distintos() -> None:
    messages = burst_messages(20, random.Random(1), 0xB5000001, first_seq=1)
    parsed = [parse_message(m) for m in messages]
    assert len({p.report_id for p in parsed}) == 20
    assert all(p.header.barrio is not None for p in parsed)


def test_rafaga_por_el_proxy_llega_al_nodo_y_no_se_repite() -> None:
    async def scenario() -> None:
        node = _node()
        _, client = _proxy(node)
        first = await client.post("/_sim/burst", json={"count": 10})
        assert first.status_code == 200
        assert first.json() == {"sent": 10, "delivered": 10, "lost": 0, "profile": "ideal"}
        await client.post("/_sim/burst", json={"count": 10})
        assert len(_reportes(node)) == 20  # cada ráfaga trae consecutivos nuevos

    asyncio.run(scenario())


def test_rafaga_con_lora_pierde_lo_que_el_canal_pierde() -> None:
    async def scenario() -> None:
        node = _node()
        _, client = _proxy(node, LORA)
        body = (await client.post("/_sim/burst", json={"count": 40})).json()
        assert body["profile"] == "lora" and body["delivered"] + body["lost"] == 40
        assert 0 < body["lost"] < 40
        assert len(_reportes(node)) == body["delivered"]

    asyncio.run(scenario())


def test_rafaga_rechaza_un_count_invalido() -> None:
    async def scenario() -> None:
        _, client = _proxy(_node())
        for bad in ({"count": 0}, {"count": 51}, {"count": "10"}, {"count": True}):
            assert (await client.post("/_sim/burst", json=bad)).status_code == 400

    asyncio.run(scenario())


def test_cors_solo_en_las_rutas_de_control_del_proxy() -> None:
    async def scenario() -> None:
        _, client = _proxy(_node())
        pre = await client.options("/_channel/profile")
        assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "*"
        assert "PUT" in pre.headers["access-control-allow-methods"]
        put = await client.put("/_channel/profile", json={"profile": "sms"})
        assert put.json()["name"] == "sms" and put.headers["access-control-allow-origin"] == "*"
        forwarded = await client.get("/api/health")  # sin Origin, el nodo no agrega CORS
        assert "access-control-allow-origin" not in forwarded.headers

    asyncio.run(scenario())


# --- Digest y apagado simulado en el nodo ---------------------------------------------------


def test_digest_cambia_con_cada_evento() -> None:
    api = TestClient(create_app(_node(), gossip_interval=None))
    before = check(api.get("/api/digest"), "Digest", "GET /api/digest")
    assert before["node"] == "A" and before["events"] == 0
    api.post("/api/messages", content=message())
    after = api.get("/api/digest").json()
    assert after["events"] > 0 and after["digest"] != before["digest"]


def test_apagado_la_api_responde_503_y_las_pantallas_siguen() -> None:
    node = _node()
    api = TestClient(create_app(node, gossip_interval=None))
    assert api.get("/_sim/power").json() == {"node": "A", "on": True}
    assert api.post("/_sim/power", json={"on": False}).json() == {"node": "A", "on": False}
    for path in ("/api/health", "/api/incidents", "/api/digest"):
        response = api.get(path)
        assert response.status_code == 503 and "apagado" in response.json()["error"]
    assert api.post("/api/messages", content=message()).status_code == 503
    assert _reportes(node) == []
    assert api.get("/tablero/").status_code == 200
    api.post("/_sim/power", json={"on": True})
    assert api.get("/api/health").status_code == 200
    assert api.post("/_sim/power", json={"on": "quizá"}).status_code == 400


def test_apagar_a_desde_la_barra_hace_que_b_tome_su_trabajo_y_al_volver_converge(
    tmp_path,
) -> None:
    async def scenario() -> None:
        cl = Cluster(tmp_path)
        received_only(cl["A"])
        await cl.spread()
        cl["A"].set_power(False)
        await cl.rounds(count=7)  # A sigue corriendo, pero calla: 14 s sin latidos
        (event,) = takeovers(cl["B"])
        assert event.payload["from_node"] == "A"
        assert takeovers(cl["A"]) == []  # apagado, A no se entera de nada
        assert not converged(cl.alive("A", "B", "C"))
        cl["A"].set_power(True)
        await cl.rounds(count=3)
        assert converged(cl.alive("A", "B", "C"))
        # Al volver, A no da por caídos a B y C por los latidos viejos.
        assert [e for e in takeovers(cl["A"]) if e.origin_node == "A"] == []
        assert cl["A"].resilience.stats.restarted

    asyncio.run(scenario())


# --- D8: RAG en la ficha --------------------------------------------------------------------


def test_la_ficha_del_nodo_cita_el_rag_ademas_de_la_matriz_emre() -> None:
    node = Node("A", SQLiteEventStore(), clock=lambda: NOW)
    TestClient(create_app(node, gossip_interval=None)).post("/api/messages", content=message())
    (ficha,) = [Ficha.model_validate(e.payload) for e in node.store.since({}) if e.type == "ficha"]
    assert len(ficha.citations) >= 2  # matriz EMRE + pasaje recuperado de data/


def test_sin_retriever_la_ficha_cita_solo_la_matriz() -> None:
    node = Node("A", SQLiteEventStore(), clock=lambda: NOW, retriever=None)
    TestClient(create_app(node, gossip_interval=None)).post("/api/messages", content=message())
    (ficha,) = [Ficha.model_validate(e.payload) for e in node.store.since({}) if e.type == "ficha"]
    assert len(ficha.citations) == 1
