"""Recepción de mensajes binarios (POST /api/messages): bytes → evento `reporte`.

Lee la cabecera de 21 B (protocol/header.py) y el texto comprimido (protocol/text.py). Los
bytes inválidos se rechazan con un mensaje claro, que el nodo devuelve como 400.
"""

from __future__ import annotations

from dataclasses import dataclass

from guadua.protocol.header import HEADER_SIZE, Header, HeaderError, MessageType, decode
from guadua.protocol.text import TextError, decode_text
from guadua.store.event_types import Channel, Reporte, report_id
from guadua.store.events import Event, EventCategory

# Nivel 1: reporte y SOS. "Estoy a salvo" y las respuestas de seguimiento son de nivel 2.
SUPPORTED_TYPES = frozenset({MessageType.REPORTE, MessageType.SOS_ESCOMBROS})


class IntakeError(ValueError):
    """Mensaje que el nodo no acepta. Su texto va en el ApiError del 400."""


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    header: Header
    text: str | None
    raw: bytes

    @property
    def report_id(self) -> str:
        return report_id(self.header.device_id, self.header.seq)


def parse_message(data: bytes) -> ParsedMessage:
    try:
        header = decode(data)
    except HeaderError as exc:
        raise IntakeError(str(exc)) from exc
    if header.msg_type not in SUPPORTED_TYPES:
        raise IntakeError(f"tipo de mensaje no soportado todavía: {header.msg_type.name.lower()}")
    try:
        text = decode_text(data[HEADER_SIZE:])
    except TextError as exc:
        raise IntakeError(
            f"texto inválido ({exc}); debe ir con el formato de protocol/text.py"
        ) from exc
    return ParsedMessage(header, text or None, bytes(data))


def reporte_event(
    message: ParsedMessage,
    *,
    node_id: str,
    seq: int,
    received_at: float,
    channel: Channel = "wifi",
) -> Event:
    header = message.header
    payload = Reporte(
        report_id=message.report_id,
        msg_type=header.msg_type,
        incident=header.incident,
        people=header.people,
        flags=int(header.flags),
        geohash=header.geohash,
        barrio=header.barrio,
        device_time=header.device_time,
        text=message.text,
        channel=channel,
        raw_hex=message.raw.hex(),
    )
    return Event(
        origin_node=node_id,
        seq=seq,
        category=EventCategory.HECHO,
        type="reporte",
        payload=payload.model_dump(mode="json"),
        received_at=received_at,
    )
