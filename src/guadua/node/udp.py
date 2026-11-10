"""Transporte por UDP entre nodos: tramas sueltas que pueden perderse, como una radio.

Implementa `Transport` para que el gossip funcione entre contenedores o equipos distintos.
Cada paquete lleva delante el nombre de quien lo envía (1 B de longitud + nombre en ASCII),
para que `receive` pueda devolver el origen; el gossip nunca ve esos bytes.

Con `profile`, cada trama pasa antes por el modelo de fallos del canal simulado (N3): así,
con Docker también se ve cómo el gossip converge con pérdida.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from guadua.channel.faults import FaultModel
from guadua.channel.profiles import ChannelProfile

logger = logging.getLogger("guadua.udp")


class UdpTransport:
    def __init__(
        self,
        node_id: str,
        listen: tuple[str, int],
        peers: dict[str, tuple[str, int]],
        *,
        mtu: int = 200,
        profile: ChannelProfile | None = None,
        seed: int = 0,
    ) -> None:
        if not node_id.isascii() or not 0 < len(node_id) < 256:
            raise ValueError(f"nombre de nodo inválido: {node_id!r}")
        self.node_id = node_id
        self.mtu = mtu
        self.listen = listen
        self.peers = dict(peers)
        self._prefix = bytes([len(node_id)]) + node_id.encode()
        self._faults = (
            FaultModel(profile, random.Random(f"{seed}:udp:{node_id}")) if profile else None
        )
        self._queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self._queue), local_addr=self.listen
        )
        self.listen = self._transport.get_extra_info("sockname")[:2]

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    async def send(self, destination: str, frame: bytes) -> None:
        if len(frame) > self.mtu:
            raise ValueError(f"trama de {len(frame)} B supera la MTU de {self.mtu} B")
        address = self.peers.get(destination)
        if address is None or self._transport is None:
            return  # sin dirección o sin arrancar: como un enlace caído
        copies = 1
        if self._faults is not None:
            verdict = self._faults.decide()
            if verdict.lost:
                return
            copies = len(verdict.delays)
        for _ in range(copies):
            with contextlib.suppress(OSError):  # p. ej., el vecino aún no resuelve por DNS
                self._transport.sendto(self._prefix + frame, address)

    async def receive(self, timeout: float) -> tuple[str, bytes] | None:
        try:
            if timeout <= 0:
                return self._queue.get_nowait()
            return await asyncio.wait_for(self._queue.get(), timeout)
        except (TimeoutError, asyncio.QueueEmpty):
            return None


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue[tuple[str, bytes]]) -> None:
        self._queue = queue

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if not data or len(data) < 1 + data[0]:
            logger.debug("paquete UDP sin origen desde %s", addr)
            return
        size = data[0]
        try:
            source = data[1 : 1 + size].decode("ascii")
        except UnicodeDecodeError:
            return
        self._queue.put_nowait((source, data[1 + size :]))
