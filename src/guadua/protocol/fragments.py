"""Fragmentos y ACK con mapa de bits. CONTRATO: ver docs/contratos.md.

Si un mensaje no cabe en la trama (`Transport.mtu`), se parte en fragmentos; el receptor
responde con un ACK que marca lo recibido y el emisor reenvía solo lo que falta.

Fragmento (11 B + datos):
    0      versión + tipo (2 fragmento · 8 fragmento de foto), igual que la cabecera de 21 B
    1–4    dispositivo (uint32)   5–8 consecutivo (uint32)
    9      índice (desde 0)       10  total (1 a 255)
    11…    datos (al menos 1 B, hasta mtu − 11)

ACK (11 B + mapa de bits):
    0      versión + tipo (3 ACK)
    1–8    dispositivo + consecutivo del mensaje confirmado
    9      tipo confirmado (cualquiera menos ACK); separa el texto de la foto con el mismo ID
    10     total
    11…    ⌈total/8⌉ B; el fragmento i es el bit i % 8 del byte i // 8 (primero el menos
           significativo). Los bits de relleno van en cero.

Un mensaje que cabe en una trama no se fragmenta y se confirma con total 1 y mapa 01.
Big-endian. Debe producir los mismos bytes que el códec JS (tests/fixtures/fragment_vectors.json).
Aquí no hay red ni temporizadores: los reintentos son de la cola (N11) y el canal de N3.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from guadua.protocol.header import MAX_UINT32, VERSION, MessageType

FRAGMENT_HEADER_SIZE = 11
ACK_HEADER_SIZE = 11
MAX_FRAGMENTS = 255
FRAGMENT_TYPES = frozenset({MessageType.FRAGMENTO, MessageType.FRAGMENTO_FOTO})

_PREFIX = struct.Struct(">BIIBB")  # byte 0, dispositivo, consecutivo, índice|tipo, total


class FragmentError(ValueError):
    """Fragmento o ACK inválido al codificar o decodificar."""


def _check_uint(name: str, value: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise FragmentError(f"{name} fuera de rango: {value!r}")


def _check_total(total: int) -> None:
    _check_uint("total", total, MAX_FRAGMENTS)
    if total == 0:
        raise FragmentError("total debe ser al menos 1")


def _message_type(name: str, value: int) -> MessageType:
    try:
        return MessageType(value)
    except ValueError as exc:
        raise FragmentError(f"{name} desconocido: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class Fragment:
    msg_type: MessageType  # FRAGMENTO o FRAGMENTO_FOTO
    device_id: int
    seq: int
    index: int
    total: int
    data: bytes

    def __post_init__(self) -> None:
        msg_type = _message_type("msg_type", self.msg_type)
        if msg_type not in FRAGMENT_TYPES:
            raise FragmentError(f"tipo de fragmento inválido: {msg_type!r}")
        object.__setattr__(self, "msg_type", msg_type)
        _check_uint("device_id", self.device_id, MAX_UINT32)
        _check_uint("seq", self.seq, MAX_UINT32)
        _check_total(self.total)
        _check_uint("index", self.index, self.total - 1)
        if not isinstance(self.data, bytes) or not self.data:
            raise FragmentError("el fragmento necesita al menos 1 B de datos")

    @property
    def key(self) -> tuple[MessageType, int, int]:
        return self.msg_type, self.device_id, self.seq


@dataclass(frozen=True, slots=True)
class Ack:
    acked_type: MessageType
    device_id: int
    seq: int
    total: int
    received: frozenset[int]

    def __post_init__(self) -> None:
        acked_type = _message_type("acked_type", self.acked_type)
        if acked_type == MessageType.ACK:
            raise FragmentError("un ACK no se confirma con otro ACK")
        object.__setattr__(self, "acked_type", acked_type)
        _check_uint("device_id", self.device_id, MAX_UINT32)
        _check_uint("seq", self.seq, MAX_UINT32)
        _check_total(self.total)
        received = frozenset(self.received)
        for index in received:
            _check_uint("índice recibido", index, self.total - 1)
        object.__setattr__(self, "received", received)

    @property
    def key(self) -> tuple[MessageType, int, int]:
        return self.acked_type, self.device_id, self.seq

    @property
    def complete(self) -> bool:
        return len(self.received) == self.total

    @property
    def missing(self) -> list[int]:
        return [i for i in range(self.total) if i not in self.received]


def _bitmap_size(total: int) -> int:
    return (total + 7) // 8


def encode_fragment(fragment: Fragment) -> bytes:
    prefix = _PREFIX.pack(
        (VERSION << 4) | fragment.msg_type,
        fragment.device_id,
        fragment.seq,
        fragment.index,
        fragment.total,
    )
    return prefix + fragment.data


def decode_fragment(data: bytes) -> Fragment:
    if len(data) <= FRAGMENT_HEADER_SIZE:
        raise FragmentError(
            f"un fragmento necesita más de {FRAGMENT_HEADER_SIZE} B, llegaron {len(data)}"
        )
    first, device_id, seq, index, total = _PREFIX.unpack_from(data)
    if first >> 4 != VERSION:
        raise FragmentError(f"versión no soportada: {first >> 4}")
    return Fragment(first & 0x0F, device_id, seq, index, total, bytes(data[FRAGMENT_HEADER_SIZE:]))


def encode_ack(ack: Ack) -> bytes:
    bitmap = bytearray(_bitmap_size(ack.total))
    for index in ack.received:
        bitmap[index // 8] |= 1 << (index % 8)
    prefix = _PREFIX.pack(
        (VERSION << 4) | MessageType.ACK, ack.device_id, ack.seq, ack.acked_type, ack.total
    )
    return prefix + bytes(bitmap)


def decode_ack(data: bytes) -> Ack:
    if len(data) < ACK_HEADER_SIZE + 1:
        raise FragmentError(f"un ACK necesita más de {ACK_HEADER_SIZE} B, llegaron {len(data)}")
    first, device_id, seq, acked_type, total = _PREFIX.unpack_from(data)
    if first >> 4 != VERSION:
        raise FragmentError(f"versión no soportada: {first >> 4}")
    if first & 0x0F != MessageType.ACK:
        raise FragmentError(f"no es un ACK: tipo {first & 0x0F}")
    _check_total(total)
    bitmap = data[ACK_HEADER_SIZE:]
    if len(bitmap) != _bitmap_size(total):
        raise FragmentError(f"mapa de {len(bitmap)} B para {total} fragmentos")
    received = {i for i in range(len(bitmap) * 8) if bitmap[i // 8] >> (i % 8) & 1}
    if any(i >= total for i in received):
        raise FragmentError("bits de relleno del mapa distintos de cero")
    return Ack(acked_type, device_id, seq, total, frozenset(received))


def fragment_capacity(mtu: int) -> int:
    """Bytes de datos que caben en cada fragmento con esta trama."""
    capacity = mtu - FRAGMENT_HEADER_SIZE
    if capacity < 1:
        raise FragmentError(f"una trama de {mtu} B no deja espacio para datos")
    return capacity


def split(
    message: bytes,
    *,
    device_id: int,
    seq: int,
    mtu: int,
    msg_type: MessageType = MessageType.FRAGMENTO,
) -> list[Fragment]:
    """Parte `message` en fragmentos que caben, con cabecera, en tramas de `mtu` bytes."""
    if not message:
        raise FragmentError("no hay nada que fragmentar")
    size = fragment_capacity(mtu)
    total = -(-len(message) // size)
    if total > MAX_FRAGMENTS:
        raise FragmentError(
            f"{len(message)} B necesitan {total} fragmentos de {size} B; el máximo es "
            f"{MAX_FRAGMENTS}"
        )
    return [
        Fragment(msg_type, device_id, seq, i, total, message[i * size : (i + 1) * size])
        for i in range(total)
    ]


@dataclass(slots=True)
class _Partial:
    total: int
    parts: dict[int, bytes] = field(default_factory=dict)
    delivered: bool = False


class Reassembler:
    """Receptor: junta fragmentos de varios mensajes y arma el ACK de cada uno.

    Un fragmento repetido no se guarda dos veces. Tras completar un mensaje se recuerda que
    está completo, para responder ACK a los repetidos sin entregarlo de nuevo.
    """

    def __init__(self) -> None:
        self._partials: dict[tuple[MessageType, int, int], _Partial] = {}

    def add(self, fragment: Fragment) -> bytes | None:
        """Guarda el fragmento. Devuelve el mensaje completo la primera vez que se completa."""
        partial = self._partials.setdefault(fragment.key, _Partial(fragment.total))
        if partial.total != fragment.total:
            raise FragmentError(f"total {fragment.total} distinto del conocido {partial.total}")
        known = partial.parts.get(fragment.index)
        if known is not None:
            if known != fragment.data:
                raise FragmentError(f"fragmento {fragment.index} repetido con otros datos")
            return None
        partial.parts[fragment.index] = fragment.data
        if partial.delivered or len(partial.parts) < partial.total:
            return None
        partial.delivered = True
        return b"".join(partial.parts[i] for i in range(partial.total))

    def ack(self, msg_type: MessageType, device_id: int, seq: int) -> Ack:
        partial = self._partials.get((_message_type("msg_type", msg_type), device_id, seq))
        if partial is None:
            raise FragmentError("no hay fragmentos de ese mensaje")
        return Ack(msg_type, device_id, seq, partial.total, frozenset(partial.parts))

    def ack_for(self, fragment: Fragment) -> Ack:
        return self.ack(*fragment.key)


class Sender:
    """Emisor de un mensaje fragmentado: con cada ACK sabe qué falta reenviar."""

    def __init__(self, fragments: list[Fragment]) -> None:
        if not fragments:
            raise FragmentError("no hay fragmentos que enviar")
        self._key = fragments[0].key
        self._total = fragments[0].total
        if len(fragments) != self._total or any(
            f.key != self._key or f.index != i for i, f in enumerate(fragments)
        ):
            raise FragmentError("los fragmentos deben ser un mensaje completo y en orden")
        self._fragments = fragments
        self._acked: set[int] = set()

    def pending(self) -> list[Fragment]:
        """Fragmentos que ningún ACK ha confirmado todavía."""
        return [f for f in self._fragments if f.index not in self._acked]

    def on_ack(self, ack: Ack) -> None:
        if ack.key != self._key or ack.total != self._total:
            raise FragmentError("el ACK es de otro mensaje")
        self._acked |= ack.received

    @property
    def done(self) -> bool:
        return len(self._acked) == self._total
