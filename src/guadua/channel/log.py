"""Registro de lo que pasa por el canal, para verlo en la terminal o en la barra de la demo.

Cada evento también sale por `logging` (logger "guadua.channel"): con nivel INFO se ve en
la terminal sin instalar nada.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

from guadua.protocol.fragments import FragmentError, decode_ack, decode_fragment
from guadua.protocol.header import VERSION, MessageType

logger = logging.getLogger("guadua.channel")

EventKind = Literal[
    "entregada", "perdida", "duplicada", "corte_inicio", "corte_fin", "sin_enlace", "cambio"
]

_NAMES = {
    MessageType.REPORTE: "reporte",
    MessageType.SOS_ESCOMBROS: "SOS escombros",
    MessageType.FRAGMENTO: "fragmento",
    MessageType.ACK: "ACK",
    MessageType.A_SALVO: "estoy a salvo",
    MessageType.LOTE_EVENTOS: "lote de eventos",
    MessageType.PREGUNTA: "pregunta",
    MessageType.RESPUESTA: "respuesta",
    MessageType.FRAGMENTO_FOTO: "fragmento foto",
}


def describe_frame(frame: bytes) -> str:
    """Nombre legible de una trama según su primer byte, p. ej. "fragmento foto 3/17"."""
    if not frame or frame[0] >> 4 != VERSION:
        return "desconocido"
    try:
        msg_type = MessageType(frame[0] & 0x0F)
    except ValueError:
        return f"tipo {frame[0] & 0x0F}"
    name = _NAMES[msg_type]
    try:
        if msg_type in (MessageType.FRAGMENTO, MessageType.FRAGMENTO_FOTO):
            fragment = decode_fragment(frame)
            return f"{name} {fragment.index + 1}/{fragment.total}"
        if msg_type == MessageType.ACK:
            ack = decode_ack(frame)
            return f"ACK {_NAMES[ack.acked_type]} ({len(ack.received)} de {ack.total})"
    except FragmentError:
        return f"{name} inválido"
    return name


@dataclass(frozen=True, slots=True)
class ChannelEvent:
    time: float  # segundos Unix
    link: str  # "A→B", "proxy ida", "proxy vuelta"
    profile: str
    kind: EventKind
    size: int = 0
    description: str = ""
    delay: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __str__(self) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.time))
        ms = int(self.time * 1000) % 1000
        extra = f" (retraso {self.delay:.2f} s)" if self.delay is not None else ""
        size = f"{self.size:>5} B" if self.size else " " * 7
        return (
            f"{stamp}.{ms:03d}  {self.link:<12} {self.profile:<6} {self.description:<28} "
            f"{size}  {self.kind}{extra}"
        )


Listener = Callable[[ChannelEvent], None]


class ChannelLog:
    """Guarda los últimos `capacity` eventos y avisa a quien se suscriba."""

    def __init__(self, capacity: int = 1000, clock: Callable[[], float] = time.time) -> None:
        self._events: deque[ChannelEvent] = deque(maxlen=capacity)
        self._listeners: list[Listener] = []
        self._clock = clock

    def record(
        self,
        link: str,
        profile: str,
        kind: EventKind,
        *,
        frame: bytes | None = None,
        description: str | None = None,
        delay: float | None = None,
    ) -> ChannelEvent:
        event = ChannelEvent(
            time=self._clock(),
            link=link,
            profile=profile,
            kind=kind,
            size=len(frame) if frame is not None else 0,
            description=description
            if description is not None
            else (describe_frame(frame) if frame is not None else ""),
            delay=delay,
        )
        self._events.append(event)
        logger.info("%s", event)
        for listener in list(self._listeners):
            listener(event)
        return event

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Llama a `listener` con cada evento nuevo. Devuelve la función para desuscribirse."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def events(self, limit: int | None = None) -> list[ChannelEvent]:
        items = list(self._events)
        return items[-limit:] if limit else items

    def __len__(self) -> int:
        return len(self._events)
