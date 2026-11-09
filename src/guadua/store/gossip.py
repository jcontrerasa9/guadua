"""Gossip con vectores de versión (docs/diseno.md §9). CONTRATO: ver docs/contratos.md.

Cada pocos segundos, cada nodo le dice a sus vecinos qué tiene (su vector de versión) y el
vecino le envía solo los eventos que le faltan. Lo perdido se vuelve a pedir en la ronda
siguiente; como los eventos son inmutables y tienen ID único, un repetido no hace daño. Así lo
que entra por A llega a C a través de B, sin enlace directo A–C.

Mensajes (tipo 5 "lote de eventos" de la cabecera; byte 0 = 0x15, luego un mapa MessagePack):
    vector  {"k": "v", "vv": {"A": 12, "B": 8}, "d": <8 primeros bytes del digest>}
    evento  {"k": "e", "ev": {...evento completo, como Event.model_dump(mode="json")...}}

Un mensaje que no cabe en la trama se parte con los fragmentos de N2. El ID del fragmento sale
del ID del evento (dispositivo = CRC32 del nodo de origen, consecutivo = seq), así que el mismo
evento reenviado en otra ronda completa los pedazos que faltaban.
"""

from __future__ import annotations

import asyncio
import logging
import time
import zlib
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import msgpack
from pydantic import ValidationError

from guadua.protocol.fragments import (
    FragmentError,
    Reassembler,
    decode_fragment,
    encode_fragment,
    split,
)
from guadua.protocol.header import VERSION, MessageType
from guadua.providers.transport import Transport
from guadua.store.event_types import EventTypeError, validate_event
from guadua.store.events import Event, EventStore, VersionVector

logger = logging.getLogger("guadua.gossip")

GOSSIP_PREFIX = (VERSION << 4) | MessageType.LOTE_EVENTOS
FRAGMENT_PREFIX = (VERSION << 4) | MessageType.FRAGMENTO
DIGEST_PREFIX_BYTES = 8
MAX_EVENTS_PER_REPLY = 50
MAX_PENDING_FRAGMENTS = 2000  # por vecino; al pasarlo se descartan los pedazos a medio juntar


class GossipError(ValueError):
    """Mensaje de gossip inválido."""


@dataclass(frozen=True, slots=True)
class VectorMessage:
    vector: VersionVector
    digest_prefix: bytes


@dataclass(frozen=True, slots=True)
class EventMessage:
    event: Event


type GossipMessage = VectorMessage | EventMessage


# --- Códec ----------------------------------------------------------------------------------


def _pack(body: dict[str, Any]) -> bytes:
    return bytes([GOSSIP_PREFIX]) + msgpack.packb(body, use_bin_type=True)


def encode_vector(vector: VersionVector, digest: str) -> bytes:
    """`digest` es el hex de `EventStore.digest()`; viajan solo sus primeros 8 bytes."""
    _check_vector(vector)
    prefix = bytes.fromhex(digest)[:DIGEST_PREFIX_BYTES]
    return _pack({"k": "v", "vv": dict(vector), "d": prefix})


def encode_event(event: Event) -> bytes:
    return _pack({"k": "e", "ev": event.model_dump(mode="json")})


def decode_message(data: bytes) -> GossipMessage:
    if not data or data[0] != GOSSIP_PREFIX:
        raise GossipError("no es un mensaje de gossip (se esperaba el byte 0x15)")
    try:
        body = msgpack.unpackb(data[1:], raw=False, strict_map_key=True)
    except (ValueError, msgpack.UnpackException) as exc:
        raise GossipError(f"MessagePack inválido: {exc}") from exc
    if not isinstance(body, dict):
        raise GossipError("el cuerpo debe ser un mapa")
    kind = body.get("k")
    if kind == "v":
        _check_keys(body, {"k", "vv", "d"})
        vector, digest_prefix = body["vv"], body["d"]
        _check_vector(vector)
        if not isinstance(digest_prefix, bytes) or len(digest_prefix) != DIGEST_PREFIX_BYTES:
            raise GossipError("el digest debe tener 8 bytes")
        return VectorMessage(dict(vector), digest_prefix)
    if kind == "e":
        _check_keys(body, {"k", "ev"})
        try:
            return EventMessage(Event.model_validate(body["ev"]))
        except ValidationError as exc:
            raise GossipError(f"evento inválido: {exc}") from exc
    raise GossipError(f"tipo de mensaje de gossip desconocido: {kind!r}")


def _check_keys(body: dict[str, Any], expected: set[str]) -> None:
    if set(body) != expected:
        raise GossipError(f"campos esperados {sorted(expected)}, llegaron {sorted(body)}")


def _check_vector(vector: Any) -> None:
    if not isinstance(vector, dict):
        raise GossipError("el vector debe ser un mapa nodo → consecutivo")
    for node, seq in vector.items():
        if not isinstance(node, str) or not node:
            raise GossipError(f"nodo inválido en el vector: {node!r}")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise GossipError(f"consecutivo inválido en el vector: {seq!r}")


def fragment_id(event: Event) -> tuple[int, int]:
    """(dispositivo, consecutivo) de los fragmentos de un evento: estable entre rondas."""
    return zlib.crc32(event.origin_node.encode()), event.seq


# --- Nodo -----------------------------------------------------------------------------------


@dataclass(slots=True)
class GossipStats:
    rounds: int = 0
    vectors_sent: int = 0
    vectors_received: int = 0
    events_sent: int = 0
    frames_sent: int = 0
    events_received: int = 0  # nuevos, guardados
    duplicates: int = 0  # ya los tenía
    invalid: int = 0  # no cumplen el catálogo o la forma de Event
    garbage: int = 0  # tramas que no se pudieron leer
    in_sync_skips: int = 0  # vectores con el mismo digest: no hubo nada que enviar

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class GossipNode:
    """Sincroniza el `store` de un nodo con sus vecinos sobre cualquier `Transport`."""

    def __init__(
        self,
        node_id: str,
        store: EventStore,
        transport: Transport,
        peers: Sequence[str],
        *,
        on_event: Callable[[Event], None] | None = None,
        max_events_per_reply: int = MAX_EVENTS_PER_REPLY,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """`monotonic` es el reloj de los latidos (`last_seen`); se inyecta en pruebas."""
        if node_id in peers:
            raise ValueError("un nodo no puede ser su propio vecino")
        self.node_id = node_id
        self.store = store
        self.transport = transport
        self.peers = tuple(peers)
        self.on_event = on_event
        self.max_events_per_reply = max_events_per_reply
        self.stats = GossipStats()
        self._reassemblers: dict[str, Reassembler] = {}
        self._pending: dict[str, int] = {}
        self.monotonic = monotonic
        self.last_seen: dict[str, float] = {}  # vecino → última vez que llegó algo suyo
        # vecino → primera vez que su vector mostró que no nos falta nada de lo que él tiene
        self.caught_up: dict[str, float] = {}

    def peers_alive(self, within: float = 10.0) -> tuple[str, ...]:
        """Vecinos de los que llegó algo en los últimos `within` segundos."""
        now = self.monotonic()
        return tuple(p for p in self.peers if now - self.last_seen.get(p, -within - 1) <= within)

    async def tick(self) -> None:
        """Una ronda: le cuenta a cada vecino qué tiene."""
        self.stats.rounds += 1
        message = encode_vector(self.store.version_vector(), self.store.digest())
        for peer in self.peers:
            await self._send(peer, message)
            self.stats.vectors_sent += 1

    async def drain(self) -> int:
        """Procesa lo que ya llegó, sin esperar. Devuelve cuántas tramas procesó."""
        count = 0
        while (item := await self.transport.receive(timeout=0)) is not None:
            await self.handle(*item)
            count += 1
        return count

    async def run(self, interval: float = 2.0, stop: asyncio.Event | None = None) -> None:
        """Ciclo del nodo: una ronda cada `interval` segundos y, entre rondas, atiende lo que
        llegue. Termina cuando se activa `stop`."""
        stop = stop or asyncio.Event()
        while not stop.is_set():
            await self.tick()
            deadline = time.monotonic() + interval
            while not stop.is_set() and (left := deadline - time.monotonic()) > 0:
                item = await self.transport.receive(timeout=min(left, 0.5))
                if item is not None:
                    await self.handle(*item)

    async def handle(self, source: str, frame: bytes) -> None:
        """Atiende una trama. Nunca lanza por datos del canal: lo que no sirve se cuenta."""
        if source in self.peers:
            self.last_seen[source] = self.monotonic()
        if not frame:
            self.stats.garbage += 1
            return
        if frame[0] == FRAGMENT_PREFIX:
            message = self._add_fragment(source, frame)
            if message is None:
                return
        else:
            message = frame
        try:
            decoded = decode_message(message)
        except GossipError as exc:
            self.stats.garbage += 1
            logger.debug("%s: trama descartada de %s: %s", self.node_id, source, exc)
            return
        if isinstance(decoded, VectorMessage):
            await self._on_vector(source, decoded)
        else:
            self._on_event(source, decoded.event)

    async def _on_vector(self, peer: str, message: VectorMessage) -> None:
        self.stats.vectors_received += 1
        if peer not in self.peers:
            return  # solo se sincroniza con vecinos conocidos
        if bytes.fromhex(self.store.digest())[:DIGEST_PREFIX_BYTES] == message.digest_prefix:
            self.stats.in_sync_skips += 1
            self.caught_up.setdefault(peer, self.monotonic())
            return
        mine = self.store.version_vector()
        if all(mine.get(origin, 0) >= seq for origin, seq in message.vector.items()):
            self.caught_up.setdefault(peer, self.monotonic())
        for event in self.store.since(message.vector)[: self.max_events_per_reply]:
            await self._send_event(peer, event)

    def _on_event(self, source: str, event: Event) -> None:
        try:
            validate_event(event)
        except EventTypeError as exc:
            self.stats.invalid += 1
            logger.warning(
                "%s: evento %s:%s inválido de %s: %s", self.node_id, *event.id, source, exc
            )
            return
        if not self.store.append(event):
            self.stats.duplicates += 1
            return
        self.stats.events_received += 1
        logger.info(
            "%s: evento %s:%s (%s) llegó de %s", self.node_id, *event.id, event.type, source
        )
        if self.on_event is not None:
            self.on_event(event)

    def _add_fragment(self, source: str, frame: bytes) -> bytes | None:
        reassembler = self._reassemblers.setdefault(source, Reassembler())
        self._pending[source] = self._pending.get(source, 0) + 1
        if self._pending[source] > MAX_PENDING_FRAGMENTS:
            self._reassemblers[source] = reassembler = Reassembler()
            self._pending[source] = 1
        try:
            return reassembler.add(decode_fragment(frame))
        except FragmentError as exc:
            self.stats.garbage += 1
            logger.debug("%s: fragmento descartado de %s: %s", self.node_id, source, exc)
            return None

    async def _send_event(self, peer: str, event: Event) -> None:
        message = encode_event(event)
        self.stats.events_sent += 1
        if len(message) <= self.transport.mtu:
            await self._send(peer, message)
            return
        device_id, seq = fragment_id(event)
        for fragment in split(message, device_id=device_id, seq=seq, mtu=self.transport.mtu):
            await self._send(peer, encode_fragment(fragment))

    async def _send(self, peer: str, frame: bytes) -> None:
        if len(frame) > self.transport.mtu:
            raise GossipError(f"mensaje de {len(frame)} B no cabe en la trama de {peer}")
        self.stats.frames_sent += 1
        await self.transport.send(peer, frame)
