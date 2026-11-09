"""Red simulada entre nodos: implementa `Transport` con perfiles, fallos y control en vivo.

    network = SimulatedNetwork(seed=1)
    network.link("A", "B", LORA)          # enlace en ambos sentidos
    a, b = network.endpoint("A"), network.endpoint("B")
    await a.send("B", trama)
    await b.receive(timeout=1.0)          # → ("A", trama), o None

Durante la demo se puede cambiar el canal sin reiniciar nada:
`update("A", "B", loss=0.6)`, `set_profile(...)`, `disconnect(...)`, `connect(...)`, y ver
qué pasa con `log` y `snapshot()`.

`time_scale` multiplica los retrasos: 1 es tiempo real; 0 entrega en cuanto se pide (útil en
pruebas que solo cuentan pérdidas). Cada sentido de cada enlace tiene su propio generador
aleatorio derivado de la semilla, así que el resultado no depende del orden entre enlaces.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from guadua.channel.faults import FaultModel
from guadua.channel.log import ChannelLog
from guadua.channel.profiles import ChannelError, ChannelProfile


@dataclass(slots=True)
class _Link:
    source: str
    destination: str
    faults: FaultModel
    connected: bool = True

    @property
    def name(self) -> str:
        return f"{self.source}→{self.destination}"


@dataclass(slots=True)
class _Inbox:
    heap: list[tuple[float, int, str, bytes]] = field(default_factory=list)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)


class SimulatedNetwork:
    def __init__(
        self,
        seed: int = 0,
        time_scale: float = 1.0,
        log: ChannelLog | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if time_scale < 0:
            raise ChannelError("time_scale no puede ser negativo")
        self.seed = seed
        self.time_scale = time_scale
        self.log = log if log is not None else ChannelLog()
        self._clock = clock
        self._links: dict[tuple[str, str], _Link] = {}
        self._inboxes: dict[str, _Inbox] = {}
        self._order = itertools.count()

    # --- Topología y control en vivo ------------------------------------------------------

    def link(self, a: str, b: str, profile: ChannelProfile) -> None:
        """Crea (o reemplaza) el enlace a ↔ b con el mismo perfil en ambos sentidos."""
        if a == b:
            raise ChannelError("un enlace necesita dos nodos distintos")
        for src, dst in ((a, b), (b, a)):
            rng = random.Random(f"{self.seed}:{src}->{dst}")
            self._links[src, dst] = _Link(src, dst, FaultModel(profile, rng))
            self._inboxes.setdefault(src, _Inbox())
            self._inboxes.setdefault(dst, _Inbox())

    def endpoint(self, node_id: str) -> SimulatedTransport:
        self._inboxes.setdefault(node_id, _Inbox())
        return SimulatedTransport(self, node_id)

    def set_profile(self, a: str, b: str, profile: ChannelProfile) -> None:
        for link in self._pair(a, b):
            link.faults.profile = profile
        self.log.record(f"{a}↔{b}", profile.name, "cambio", description=f"perfil {profile.name}")

    def update(self, a: str, b: str, **changes: Any) -> ChannelProfile:
        """Cambia parámetros del enlace en vivo, p. ej. `update("A", "B", loss=0.6)`."""
        links = self._pair(a, b)
        profile = links[0].faults.profile.with_changes(**changes)
        for link in links:
            link.faults.profile = profile
        detail = ", ".join(f"{k}={v}" for k, v in changes.items())
        self.log.record(f"{a}↔{b}", profile.name, "cambio", description=detail)
        return profile

    def disconnect(self, a: str, b: str) -> None:
        self._set_connected(a, b, False)

    def connect(self, a: str, b: str) -> None:
        self._set_connected(a, b, True)

    def profile(self, a: str, b: str) -> ChannelProfile:
        return self._pair(a, b)[0].faults.profile

    def _set_connected(self, a: str, b: str, connected: bool) -> None:
        links = self._pair(a, b)
        for link in links:
            link.connected = connected
        kind = "reconectado" if connected else "desconectado"
        self.log.record(f"{a}↔{b}", links[0].faults.profile.name, "cambio", description=kind)

    def _pair(self, a: str, b: str) -> list[_Link]:
        try:
            return [self._links[a, b], self._links[b, a]]
        except KeyError as exc:
            raise ChannelError(f"no hay enlace entre {a} y {b}") from exc

    # --- Observación -----------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Estado del canal en un dict apto para JSON: perfiles, contadores y cortes."""
        return {
            "seed": self.seed,
            "time_scale": self.time_scale,
            "links": [
                {
                    "link": link.name,
                    "connected": link.connected,
                    "in_cut": link.faults.in_cut,
                    "profile": link.faults.profile.as_dict(),
                    "stats": link.faults.stats.as_dict(),
                }
                for link in self._links.values()
            ],
        }

    def stats(self, source: str, destination: str) -> Any:
        return self._links[source, destination].faults.stats

    # --- Envío y recepción -----------------------------------------------------------------

    def mtu_of(self, node_id: str) -> int:
        mtus = [lk.faults.profile.mtu for (src, _), lk in self._links.items() if src == node_id]
        return min(mtus) if mtus else 0

    def _send(self, source: str, destination: str, frame: bytes) -> None:
        link = self._links.get((source, destination))
        if link is None or not link.connected:
            profile = link.faults.profile.name if link else "-"
            self.log.record(f"{source}→{destination}", profile, "sin_enlace", frame=frame)
            return
        profile = link.faults.profile
        if len(frame) > profile.mtu:
            raise ValueError(f"trama de {len(frame)} B supera la MTU de {profile.mtu} B")
        verdict = link.faults.decide()
        if verdict.cut_started:
            self.log.record(link.name, profile.name, "corte_inicio")
        if verdict.cut_ended:
            self.log.record(link.name, profile.name, "corte_fin")
        if verdict.lost:
            self.log.record(link.name, profile.name, "perdida", frame=frame)
            return
        inbox = self._inboxes[destination]
        now = self._clock()
        for copy, delay in enumerate(verdict.delays):
            kind = "duplicada" if copy else "entregada"
            self.log.record(link.name, profile.name, kind, frame=frame, delay=delay)
            due = now + delay * self.time_scale
            heapq.heappush(inbox.heap, (due, next(self._order), source, frame))
        inbox.arrived.set()

    async def _receive(self, node_id: str, timeout: float) -> tuple[str, bytes] | None:
        inbox = self._inboxes[node_id]
        deadline = self._clock() + timeout
        while True:
            now = self._clock()
            if inbox.heap and inbox.heap[0][0] <= now:
                _, _, source, frame = heapq.heappop(inbox.heap)
                return source, frame
            wait = deadline - now
            if wait <= 0:
                return None
            if inbox.heap:
                wait = min(wait, inbox.heap[0][0] - now)
            inbox.arrived.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(inbox.arrived.wait(), wait)


class SimulatedTransport:
    """Un nodo de la red simulada. Cumple `guadua.providers.transport.Transport`."""

    def __init__(self, network: SimulatedNetwork, node_id: str) -> None:
        self.network = network
        self.node_id = node_id

    @property
    def mtu(self) -> int:
        """La trama más pequeña entre los enlaces de este nodo."""
        return self.network.mtu_of(self.node_id)

    async def send(self, destination: str, frame: bytes) -> None:
        self.network._send(self.node_id, destination, frame)

    async def receive(self, timeout: float) -> tuple[str, bytes] | None:
        return await self.network._receive(self.node_id, timeout)
