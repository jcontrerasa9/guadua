"""Cabecera binaria de 21 B. CONTRATO: ver docs/diseno.md §5.2 (Contrato de la cabecera).

Debe producir los mismos bytes que citizen/protocol/header.js (tests/fixtures/header_vectors.json).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag

HEADER_SIZE = 21
VERSION = 1
EPOCH_UNIX = 1_767_225_600  # 2026-01-01T00:00:00Z
GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"
GEOHASH_LEN = 7
MAX_UINT32 = 2**32 - 1
MAX_UINT40 = 2**40 - 1

# Byte 0, dispositivo, consecutivo, hora, incidente, personas, banderas (16 B) + ubicación (5 B).
_FIXED = struct.Struct(">BIIIBBB")


class MessageType(IntEnum):
    REPORTE = 0
    SOS_ESCOMBROS = 1
    FRAGMENTO = 2
    ACK = 3
    A_SALVO = 4
    LOTE_EVENTOS = 5
    PREGUNTA = 6
    RESPUESTA = 7
    FRAGMENTO_FOTO = 8


class Incident(IntEnum):
    ATRAPADOS = 0
    COLAPSO = 1
    INCENDIO = 2
    FUGA_GAS = 3
    HERIDO = 4
    VIA_BLOQUEADA = 5
    OTRO = 6


class People(IntEnum):
    UNA = 0
    DE_2_A_5 = 1
    DE_6_A_20 = 2
    MAS_DE_20 = 3
    NO_SE = 4


class Flag(IntFlag):
    ATRAPADOS = 1 << 0
    NO_RESPIRA_O_SANGRADO = 1 << 1
    INCENDIO = 1 << 2
    RIESGO_ESCENA = 1 << 3
    NINOS_O_MAYORES = 1 << 4
    OBSERVACION_DIRECTA = 1 << 5
    GPS = 1 << 6
    BATERIA_CRITICA = 1 << 7


class HeaderError(ValueError):
    """Cabecera inválida al codificar o decodificar."""


def _uint(name: str, value: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise HeaderError(f"{name} fuera de rango: {value!r}")


def _enum[E: IntEnum](name: str, cls: type[E], value: int) -> E:
    try:
        return cls(value)
    except ValueError as exc:
        raise HeaderError(f"{name} desconocido: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class Header:
    msg_type: MessageType
    device_id: int
    seq: int
    device_time: int  # segundos desde EPOCH_UNIX; dato con incertidumbre, nunca ordena
    incident: Incident
    people: People
    flags: Flag
    geohash: str | None = None  # obligatorio si Flag.GPS
    barrio: int | None = None  # obligatorio si no hay Flag.GPS

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(self, "msg_type", _enum("msg_type", MessageType, self.msg_type))
        set_(self, "incident", _enum("incident", Incident, self.incident))
        set_(self, "people", _enum("people", People, self.people))
        _uint("flags", self.flags, 0xFF)
        set_(self, "flags", Flag(self.flags))
        _uint("device_id", self.device_id, MAX_UINT32)
        _uint("seq", self.seq, MAX_UINT32)
        _uint("device_time", self.device_time, MAX_UINT32)
        if Flag.GPS in self.flags:
            if self.barrio is not None:
                raise HeaderError("con GPS no se envía barrio")
            _geohash_to_int(self.geohash)
        else:
            if self.geohash is not None:
                raise HeaderError("sin la bandera GPS no se envía geohash")
            if self.barrio is None:
                raise HeaderError("sin GPS se requiere el código de barrio")
            _uint("barrio", self.barrio, MAX_UINT40)


def _geohash_to_int(geohash: str | None) -> int:
    if not isinstance(geohash, str) or len(geohash) != GEOHASH_LEN:
        raise HeaderError(f"geohash debe tener {GEOHASH_LEN} caracteres: {geohash!r}")
    value = 0
    for char in geohash:
        index = GEOHASH_ALPHABET.find(char)
        if index < 0:
            raise HeaderError(f"carácter de geohash inválido: {char!r}")
        value = value * 32 + index
    return value


def _int_to_geohash(value: int) -> str:
    chars = []
    for _ in range(GEOHASH_LEN):
        chars.append(GEOHASH_ALPHABET[value % 32])
        value //= 32
    return "".join(reversed(chars))


def encode(header: Header) -> bytes:
    """Devuelve exactamente 21 bytes."""
    gps = Flag.GPS in header.flags
    location = _geohash_to_int(header.geohash) << 5 if gps else header.barrio
    fixed = _FIXED.pack(
        (VERSION << 4) | header.msg_type,
        header.device_id,
        header.seq,
        header.device_time,
        header.incident,
        header.people,
        header.flags,
    )
    return fixed + location.to_bytes(5, "big")


def decode(data: bytes) -> Header:
    """Lee los primeros 21 bytes. El texto opcional que siga no se interpreta aquí."""
    if len(data) < HEADER_SIZE:
        raise HeaderError(f"se esperaban al menos {HEADER_SIZE} bytes, llegaron {len(data)}")
    first, device_id, seq, device_time, incident, people, flags = _FIXED.unpack_from(data)
    if first >> 4 != VERSION:
        raise HeaderError(f"versión no soportada: {first >> 4}")
    location = int.from_bytes(data[_FIXED.size : HEADER_SIZE], "big")
    geohash = barrio = None
    if flags & Flag.GPS:
        if location & 0x1F:
            raise HeaderError("los 5 bits finales del geohash deben ser cero")
        geohash = _int_to_geohash(location >> 5)
    else:
        barrio = location
    return Header(
        first & 0x0F, device_id, seq, device_time, incident, people, flags, geohash, barrio
    )
