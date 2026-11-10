"""Proxy HTTP de fallos para el tramo celular → nodo (docs/diseno.md §5.1).

Se pone entre la PWA y el nodo y reenvía cada petición aplicando un perfil:
- **Pérdida de ida:** la petición no llega al nodo → 502.
- **Pérdida de vuelta:** el nodo ya la procesó pero la respuesta se pierde → 504. Obliga a la
  PWA a reintentar y prueba que el nodo reconoce el duplicado.
- **Retraso**, **duplicado** (el nodo la recibe dos veces) y **cortes**, como en la red simulada.

Rutas propias para la demo (no son de la API del nodo):
    GET /_channel/stats     parámetros y contadores
    GET /_channel/log       últimos eventos (?limit=50)
    GET /_channel/profile   perfil activo
    PUT /_channel/profile   cambia parámetros: {"loss": 0.6} o {"profile": "sms", ...}
    POST /_sim/burst        ráfaga de reportes sintéticos al nodo por este canal (D9):
                            {"count": 10} → cuántos entregó y cuántos perdió el canal

Las rutas de control tienen CORS abierto: la barra del tablero (servido por un nodo) las usa.

Uso: python -m guadua.channel.proxy --upstream http://localhost:8000 --profile wifi --seed 1
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from guadua.channel.burst import MAX_BURST, burst_messages
from guadua.channel.faults import FaultModel
from guadua.channel.log import ChannelLog
from guadua.channel.profiles import WIFI, ChannelError, ChannelProfile, profile_by_name

CONTROL_PREFIX = "/_channel"
SIM_PREFIX = "/_sim"
_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, PUT, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}
_HOP_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "keep-alive"}


class FaultProxy:
    def __init__(
        self,
        upstream: str,
        profile: ChannelProfile = WIFI,
        *,
        seed: int = 0,
        time_scale: float = 1.0,
        client: httpx.AsyncClient | None = None,
        log: ChannelLog | None = None,
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self.time_scale = time_scale
        self.log = log if log is not None else ChannelLog()
        self.outbound = FaultModel(profile, random.Random(f"{seed}:proxy ida"))
        self.inbound = FaultModel(profile, random.Random(f"{seed}:proxy vuelta"))
        self._client = client or httpx.AsyncClient(timeout=30)
        self.seed = seed
        # Ráfagas (D9): un "dispositivo" por arranque, para no chocar con las de antes.
        self._burst_rng = random.Random(f"{seed}:rafaga")
        self._burst_device = 0xB5000000 | (int(time.time()) & 0xFFFFFF)
        self._burst_seq = 0
        self.app = Starlette(
            routes=[
                Route(f"{CONTROL_PREFIX}/stats", _cors(self._stats), methods=["GET", "OPTIONS"]),
                Route(f"{CONTROL_PREFIX}/log", _cors(self._log), methods=["GET", "OPTIONS"]),
                Route(
                    f"{CONTROL_PREFIX}/profile",
                    _cors(self._profile),
                    methods=["GET", "PUT", "OPTIONS"],
                ),
                Route(f"{SIM_PREFIX}/burst", _cors(self._burst), methods=["POST", "OPTIONS"]),
                Route("/{path:path}", self._forward, methods=_METHODS),
            ]
        )

    # --- Control en vivo -------------------------------------------------------------------

    @property
    def profile(self) -> ChannelProfile:
        return self.outbound.profile

    def set_profile(self, profile: ChannelProfile) -> None:
        self.outbound.profile = self.inbound.profile = profile
        self.log.record("proxy", profile.name, "cambio", description=f"perfil {profile.name}")

    def update(self, **changes: Any) -> ChannelProfile:
        """Cambia parámetros en vivo. `profile="sms"` parte de otro perfil base."""
        base = self.profile
        if "profile" in changes:
            name = changes.pop("profile")
            if not isinstance(name, str):
                raise ChannelError(f"perfil inválido: {name!r}")
            base = profile_by_name(name)
        profile = base.with_changes(**changes)
        self.outbound.profile = self.inbound.profile = profile
        detail = ", ".join(f"{k}={v}" for k, v in changes.items()) or f"perfil {profile.name}"
        self.log.record("proxy", profile.name, "cambio", description=detail)
        return profile

    def snapshot(self) -> dict[str, Any]:
        return {
            "upstream": self.upstream,
            "seed": self.seed,
            "time_scale": self.time_scale,
            "profile": self.profile.as_dict(),
            "in_cut": self.outbound.in_cut,
            "outbound": self.outbound.stats.as_dict(),
            "inbound": self.inbound.stats.as_dict(),
        }

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Rutas de control ------------------------------------------------------------------

    async def _stats(self, request: Request) -> Response:
        return JSONResponse(self.snapshot())

    async def _log(self, request: Request) -> Response:
        try:
            limit = int(request.query_params.get("limit", "100"))
        except ValueError:
            return _error("limit debe ser un entero")
        return JSONResponse({"events": [e.as_dict() for e in self.log.events(max(limit, 0))]})

    async def _profile(self, request: Request) -> Response:
        if request.method == "PUT":
            try:
                changes = await request.json()
            except ValueError:
                return _error("el cuerpo debe ser JSON")
            if not isinstance(changes, dict) or not changes:
                return _error("se esperaba un objeto con parámetros")
            try:
                self.update(**changes)
            except ChannelError as exc:
                return _error(str(exc))
        return JSONResponse(self.profile.as_dict())

    async def _burst(self, request: Request) -> Response:
        try:
            body = await request.json() if await request.body() else {}
        except ValueError:
            return _error("el cuerpo debe ser JSON")
        count = body.get("count", 10) if isinstance(body, dict) else None
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_BURST:
            return _error(f"count debe ser un entero de 1 a {MAX_BURST}")
        return JSONResponse(await self.burst(count))

    async def burst(self, count: int) -> dict[str, Any]:
        """Envía `count` reportes sintéticos al nodo, cada uno con los fallos del canal (sin
        reintentos: lo que el canal pierde se pierde). Van en paralelo, como varios celulares."""
        messages = burst_messages(
            count, self._burst_rng, self._burst_device, first_seq=self._burst_seq + 1
        )
        self._burst_seq += count
        results = await asyncio.gather(*(self._send_report(m) for m in messages))
        delivered = sum(r == "entregada" for r in results)
        return {
            "sent": count,
            "delivered": delivered,
            "lost": count - delivered,
            "profile": self.profile.name,
        }

    async def _send_report(self, data: bytes) -> str:
        name, label = self.profile.name, "POST /api/messages (ráfaga)"
        out = self.outbound.decide()
        self._record_cuts(out, "proxy ida")
        if out.lost:
            self.log.record("proxy ida", name, "perdida", description=label, delay=None)
            return "perdida"
        await self._sleep(out.delays[0])
        url = self.upstream + "/api/messages"
        headers = {"Content-Type": "application/octet-stream"}
        try:
            response = await self._client.post(url, content=data, headers=headers)
            for _ in out.delays[1:]:
                self.log.record("proxy ida", name, "duplicada", description=label)
                await self._client.post(url, content=data, headers=headers)
        except httpx.HTTPError:
            return "sin_nodo"
        if response.status_code != 200:
            return "rechazada"
        self.log.record("proxy ida", name, "entregada", description=label, delay=out.delays[0])
        return "entregada"

    # --- Reenvío con fallos ----------------------------------------------------------------

    async def _forward(self, request: Request) -> Response:
        body = await request.body()
        name = self.profile.name
        label = f"{request.method} {request.url.path}"
        out = self.outbound.decide()
        self._record_cuts(out, "proxy ida")
        if out.lost:
            self.log.record("proxy ida", name, "perdida", description=label, delay=None)
            return _error("simulado: la petición se perdió en el canal", 502)
        await self._sleep(out.delays[0])
        try:
            response = await self._send_upstream(request, body)
            for _ in out.delays[1:]:
                self.log.record("proxy ida", name, "duplicada", description=label)
                await self._send_upstream(request, body)
        except httpx.HTTPError as exc:
            return _error(f"el nodo no responde: {exc.__class__.__name__}", 502)
        self.log.record("proxy ida", name, "entregada", description=label, delay=out.delays[0])

        back = self.inbound.decide()
        self._record_cuts(back, "proxy vuelta")
        if back.lost:
            self.log.record("proxy vuelta", name, "perdida", description=label)
            return _error("simulado: la respuesta se perdió en el canal", 504)
        await self._sleep(back.delays[0])
        self.log.record("proxy vuelta", name, "entregada", description=label, delay=back.delays[0])
        headers = {k: v for k, v in response.headers.items() if k.lower() not in _HOP_HEADERS}
        headers.pop("content-encoding", None)
        return Response(response.content, response.status_code, headers=headers)

    async def _send_upstream(self, request: Request, body: bytes) -> httpx.Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
        url = self.upstream + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        return await self._client.request(request.method, url, content=body, headers=headers)

    def _record_cuts(self, verdict: Any, link: str) -> None:
        if verdict.cut_started:
            self.log.record(link, self.profile.name, "corte_inicio")
        if verdict.cut_ended:
            self.log.record(link, self.profile.name, "corte_fin")

    async def _sleep(self, delay: float) -> None:
        if delay and self.time_scale:
            await asyncio.sleep(delay * self.time_scale)


_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _cors(
    handler: Callable[[Request], Awaitable[Response]],
) -> Callable[[Request], Awaitable[Response]]:
    """CORS solo en las rutas de control: lo reenviado ya trae el CORS del nodo."""

    async def wrapped(request: Request) -> Response:
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=_CORS)
        response = await handler(request)
        response.headers.update(_CORS)
        return response

    return wrapped


def main(argv: list[str] | None = None) -> None:
    import logging

    import uvicorn

    parser = argparse.ArgumentParser(description="Proxy HTTP de fallos entre la PWA y un nodo")
    parser.add_argument("--upstream", required=True, help="URL del nodo, p. ej. http://node-a:8000")
    parser.add_argument("--profile", default="wifi", help="wifi, lora, sms o ideal")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--time-scale", type=float, default=1.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    proxy = FaultProxy(
        args.upstream, profile_by_name(args.profile), seed=args.seed, time_scale=args.time_scale
    )
    uvicorn.run(proxy.app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
