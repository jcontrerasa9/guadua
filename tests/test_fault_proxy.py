import asyncio

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from guadua.channel.profiles import IDEAL, LORA, WIFI
from guadua.channel.proxy import FaultProxy

UPSTREAM = "http://nodo"


class FakeNode:
    """Nodo mínimo: guarda cada mensaje recibido y reconoce duplicados por su contenido."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.app = Starlette(
            routes=[
                Route("/api/messages", self._messages, methods=["POST"]),
                Route("/api/health", self._health, methods=["GET"]),
            ]
        )

    async def _messages(self, request: Request) -> JSONResponse:
        body = await request.body()
        duplicate = body in self.received
        self.received.append(body)
        return JSONResponse({"status": "recibido", "duplicate": duplicate})

    async def _health(self, request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "q": request.query_params.get("q")})


def _setup(profile=IDEAL, seed=1) -> tuple[FakeNode, FaultProxy, httpx.AsyncClient]:
    node = FakeNode()
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=node.app), base_url=UPSTREAM)
    proxy = FaultProxy(UPSTREAM, profile, seed=seed, time_scale=0, client=upstream)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://p")
    return node, proxy, client


def _run(coro):
    return asyncio.run(coro)


def test_ideal_profile_forwards_unchanged() -> None:
    async def scenario() -> None:
        node, _, client = _setup(IDEAL)
        response = await client.post("/api/messages", content=b"\x10" + bytes(20))
        assert response.status_code == 200
        assert response.json() == {"status": "recibido", "duplicate": False}
        assert node.received == [b"\x10" + bytes(20)]
        health = await client.get("/api/health?q=1")
        assert health.json() == {"ok": True, "q": "1"}

    _run(scenario())


def test_measured_loss_per_leg_matches_configured() -> None:
    async def scenario() -> tuple[dict, dict, int]:
        node, proxy, client = _setup(IDEAL.with_loss(0.3), seed=8)
        codes = [(await client.post("/api/messages", content=bytes([i % 256]))).status_code
                 for i in range(1500)]  # fmt: skip
        snapshot = proxy.snapshot()
        assert codes.count(502) == snapshot["outbound"]["lost"]
        assert codes.count(504) == snapshot["inbound"]["lost"]
        return snapshot["outbound"], snapshot["inbound"], len(node.received)

    outbound, inbound, stored = _run(scenario())
    assert abs(outbound["measured_loss"] - 0.3) <= 0.05
    assert abs(inbound["measured_loss"] - 0.3) <= 0.05
    assert stored == outbound["sent"] - outbound["lost"]  # lo que pasó la ida llegó al nodo


def test_lost_response_leaves_message_stored_and_retry_is_duplicate() -> None:
    """Con la vuelta perdida, el nodo ya guardó el mensaje; el reintento llega como duplicado."""

    async def scenario() -> None:
        node, proxy, client = _setup(IDEAL)
        proxy.inbound.profile = IDEAL.with_loss(1.0)
        first = await client.post("/api/messages", content=b"reporte")
        assert first.status_code == 504
        assert node.received == [b"reporte"]
        proxy.inbound.profile = IDEAL
        retry = await client.post("/api/messages", content=b"reporte")
        assert retry.json()["duplicate"] is True

    _run(scenario())


def test_duplicate_reaches_node_twice() -> None:
    async def scenario() -> None:
        node, proxy, client = _setup(IDEAL.with_changes(duplicate=1.0))
        response = await client.post("/api/messages", content=b"sos")
        assert response.status_code == 200
        assert node.received == [b"sos", b"sos"]
        kinds = [e.kind for e in proxy.log.events()]
        assert "duplicada" in kinds

    _run(scenario())


def test_unreachable_node_returns_502() -> None:
    async def scenario() -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("sin red", request=request)

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        proxy = FaultProxy(UPSTREAM, IDEAL, time_scale=0, client=upstream)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://p"
        )
        response = await client.get("/api/health")
        assert response.status_code == 502
        assert "no responde" in response.json()["error"]

    _run(scenario())


# --- Rutas de control para la demo ---------------------------------------------------------


def test_control_routes_read_and_change_profile() -> None:
    async def scenario() -> None:
        node, proxy, client = _setup(WIFI)
        assert (await client.get("/_channel/profile")).json()["name"] == "wifi"

        changed = await client.put("/_channel/profile", json={"loss": 0.6, "delay_max": 2.0})
        assert changed.status_code == 200
        assert changed.json()["loss"] == 0.6
        assert proxy.profile.loss == 0.6 and proxy.inbound.profile.loss == 0.6

        switched = await client.put("/_channel/profile", json={"profile": "lora"})
        assert switched.json()["name"] == "lora"
        assert proxy.profile == LORA

        await client.post("/api/messages", content=b"x")
        stats = (await client.get("/_channel/stats")).json()
        assert stats["profile"]["name"] == "lora"
        assert stats["outbound"]["sent"] == 1

        log = (await client.get("/_channel/log?limit=2")).json()["events"]
        assert len(log) == 2
        events = (await client.get("/_channel/log")).json()["events"]
        assert [e["kind"] for e in events][:2] == ["cambio", "cambio"]
        assert node.received in ([], [b"x"])  # depende de la pérdida, pero nunca más de uno

    _run(scenario())


@pytest.mark.parametrize(
    "body",
    [{"loss": 1.5}, {"perdida": 0.3}, {"profile": "satelite"}, {"profile": 3}, {}, [1, 2]],
)
def test_control_rejects_invalid_changes(body) -> None:
    async def scenario() -> None:
        _, proxy, client = _setup(WIFI)
        response = await client.put("/_channel/profile", json=body)
        assert response.status_code == 400
        assert "error" in response.json()
        assert proxy.profile == WIFI  # nada a medias

    _run(scenario())


def test_control_rejects_bad_json_and_limit() -> None:
    async def scenario() -> None:
        _, _, client = _setup(WIFI)
        bad = await client.put("/_channel/profile", content=b"{no es json")
        assert bad.status_code == 400
        assert (await client.get("/_channel/log?limit=abc")).status_code == 400

    _run(scenario())
