"""Recepción de mensajes binarios (POST /api/messages): bytes → evento `reporte` o
`respuesta_seguimiento`.

Lee la cabecera de 21 B (protocol/header.py) y el texto comprimido (protocol/text.py). La
respuesta de seguimiento (tipo 7) tiene su propio formato tras la cabecera
(docs/contratos.md). Los bytes inválidos se rechazan con un mensaje claro, que el nodo
devuelve como 400.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from guadua.protocol.header import HEADER_SIZE, Header, HeaderError, MessageType, decode
from guadua.protocol.text import TextError, decode_text
from guadua.store.event_types import (
    EVENT_ID_PATTERN,
    MAX_OPTIONS,
    MAX_QUESTION_ID,
    AnswerKind,
    Channel,
    PreguntaSeguimiento,
    Reporte,
    RespuestaSeguimiento,
    report_id,
)
from guadua.store.events import Event, EventCategory

# Reporte y SOS (nivel 1) y respuesta de seguimiento (D5a). "Estoy a salvo" aún no.
SUPPORTED_TYPES = frozenset({MessageType.REPORTE, MessageType.SOS_ESCOMBROS, MessageType.RESPUESTA})

# Byte 0 de la respuesta (docs/contratos.md, "Respuesta de seguimiento").
ANSWER_CODES: dict[int, AnswerKind] = {0: "no", 1: "si", 2: "no_se"}
OPTION_CODE = 0x10
TEXT_CODE = 0x20
_QUESTION_ID = re.compile(EVENT_ID_PATTERN)


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


@dataclass(frozen=True, slots=True)
class ParsedAnswer:
    """Respuesta de seguimiento (tipo 7). Su ID (dispositivo + consecutivo) va en el ACK."""

    header: Header
    answer: RespuestaSeguimiento
    raw: bytes

    @property
    def report_id(self) -> str:
        return report_id(self.header.device_id, self.header.seq)

    @property
    def device(self) -> str:
        return f"{self.header.device_id:08x}"


def parse_message(data: bytes) -> ParsedMessage | ParsedAnswer:
    try:
        header = decode(data)
    except HeaderError as exc:
        raise IntakeError(str(exc)) from exc
    if header.msg_type not in SUPPORTED_TYPES:
        raise IntakeError(f"tipo de mensaje no soportado todavía: {header.msg_type.name.lower()}")
    if header.msg_type == MessageType.RESPUESTA:
        return ParsedAnswer(header, parse_answer(data[HEADER_SIZE:]), bytes(data))
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


def _question_id(raw: bytes) -> str:
    if not 1 <= len(raw) <= MAX_QUESTION_ID:
        raise IntakeError(f"question_id de {len(raw)} B (se esperan de 1 a {MAX_QUESTION_ID})")
    try:
        qid = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntakeError("question_id no es UTF-8") from exc
    if not _QUESTION_ID.fullmatch(qid):
        raise IntakeError(f"question_id inválido: {qid!r} (se espera el id de la pregunta, A:12)")
    return qid


def parse_answer(body: bytes) -> RespuestaSeguimiento:
    """Lo que va tras la cabecera de una respuesta de seguimiento (docs/contratos.md)."""
    if not body:
        raise IntakeError("respuesta vacía")
    code, rest = body[0], body[1:]
    kind: AnswerKind
    option = text = None
    if code in ANSWER_CODES:
        kind, qid = ANSWER_CODES[code], _question_id(rest)
    elif OPTION_CODE <= code < OPTION_CODE + MAX_OPTIONS:
        kind, option, qid = "opcion", code - OPTION_CODE, _question_id(rest)
    elif code == TEXT_CODE:
        if not rest:
            raise IntakeError("respuesta de texto sin question_id")
        size = rest[0]
        qid = _question_id(rest[1 : 1 + size])
        try:
            text = decode_text(rest[1 + size :]).strip() or None
        except TextError as exc:
            raise IntakeError(f"texto de la respuesta inválido ({exc})") from exc
        if text is None:
            raise IntakeError("respuesta de texto vacía")
        kind = "texto"
    else:
        raise IntakeError(f"código de respuesta desconocido: {code:#04x}")
    return RespuestaSeguimiento(question_id=qid, answer=kind, option=option, text=text)


def respuesta_event(message: ParsedAnswer, *, node_id: str, seq: int, received_at: float) -> Event:
    node, question_seq = message.answer.question_id.split(":")
    return Event(
        origin_node=node_id,
        seq=seq,
        category=EventCategory.HECHO,
        type="respuesta_seguimiento",
        payload=message.answer.model_dump(mode="json"),
        refs=((node, int(question_seq)),),
        received_at=received_at,
    )


ANSWERS_BY_KIND: dict[str, frozenset[str]] = {
    "si_no": frozenset({"si", "no", "no_se"}),
    "opciones": frozenset({"opcion", "no_se"}),
    "texto": frozenset({"texto", "no_se"}),
}


def check_answer(question: PreguntaSeguimiento, message: ParsedAnswer) -> None:
    """La respuesta debe venir del dispositivo al que se preguntó y ser del tipo de la
    pregunta. Lanza IntakeError (→ 400)."""
    if not question.report_id.startswith(message.device + "-"):
        raise IntakeError("la pregunta no es de este dispositivo")
    answer = message.answer
    if answer.answer not in ANSWERS_BY_KIND[question.kind]:
        raise IntakeError(f"respuesta {answer.answer!r} para una pregunta de tipo {question.kind}")
    if answer.option is not None and answer.option >= len(question.options):
        raise IntakeError(f"la pregunta no tiene la opción {answer.option}")
