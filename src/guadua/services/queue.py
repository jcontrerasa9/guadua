"""Cola de comunicación con envejecimiento (Agente 4).

Ordena las transmisiones según la política de docs/diseno.md §8:
    prioridad_efectiva = peso_urgencia + factor_envejecimiento × minutos_en_cola

- Pesos iniciales: Crítica 100, Alta 70, Media 40, Baja 10.
- Mensajes de control ("Estoy a salvo", ACK, SOS escombros): peso prioritario (110).
- Fragmentos de foto: peso mínimo (5), nunca retrasan un texto.
- Desempate a igual prioridad: sale primero el más pequeño (menor cantidad de bytes).
- Envejecimiento: cada minuto en cola suma prioridad, acotando la espera máxima de los bajos.
- Vencimiento sin borrado: tras superar `ttl_seconds` sin confirmación pasa a "por_confirmar".
- Reintento con espera creciente (N11b): un mensaje enviado que espera su ACK no se vuelve a
  elegir hasta que pase su espera: `retry_base_s` tras el primer envío, el doble en cada
  reintento, hasta `retry_max_s`. Mientras tanto, la ventana es para el siguiente mensaje.
- Integración con N2 (fragmentos/ACK) y N3 (Transport).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from guadua.classifier.urgency import Urgency
from guadua.protocol.fragments import (
    Ack,
    Sender,
    decode_ack,
    encode_fragment,
    split,
)
from guadua.protocol.header import HEADER_SIZE, MessageType, decode
from guadua.providers.transport import Transport

URGENCY_WEIGHTS: dict[Urgency, float] = {
    Urgency.CRITICA: 100.0,
    Urgency.ALTA: 70.0,
    Urgency.MEDIA: 40.0,
    Urgency.BAJA: 10.0,
}

CONTROL_WEIGHT: float = 110.0  # ACK, A_SALVO, SOS_ESCOMBROS
PHOTO_WEIGHT: float = 5.0  # Los fragmentos de foto nunca retrasan un texto
DEFAULT_AGING_FACTOR: float = 3.0  # puntos por minuto en cola
DEFAULT_TTL_SECONDS: float = 7200.0  # 2 horas antes de pasar a "por_confirmar"
DEFAULT_RETRY_BASE_S: float = 2.0  # espera antes del primer reintento sin ACK
# Tope de la espera. Medido con el banco (N15, semillas 1–4 y 15): frente a 30 o 60 s gasta
# las mismas tramas y acota mejor el p95 de los críticos con mucha pérdida.
DEFAULT_RETRY_MAX_S: float = 16.0


class ItemStatus(StrEnum):
    ENCOLADO = "encolado"
    TRANSMITIENDO = "transmitiendo"
    ENTREGADO = "entregado"
    POR_CONFIRMAR = "por_confirmar"


@dataclass(slots=True)
class QueueItem:
    id: int
    payload: bytes
    destination: str
    urgency: Urgency
    msg_type: MessageType
    device_id: int
    seq: int
    base_weight: float
    created_at: float
    requires_ack: bool
    ttl_seconds: float
    status: ItemStatus = ItemStatus.ENCOLADO
    sender: Sender | None = None
    attempts: int = 0
    last_attempt_at: float | None = None

    def effective_priority(self, now: float, aging_factor: float) -> float:
        """Calcula la prioridad efectiva sumando el envejecimiento por minutos en cola."""
        minutes_in_queue = max(0.0, (now - self.created_at) / 60.0)
        return self.base_weight + (aging_factor * minutes_in_queue)

    @property
    def key(self) -> tuple[MessageType, int, int]:
        return self.msg_type, self.device_id, self.seq


class CommunicationQueue:
    """Cola de transmisión con envejecimiento del Agente 4."""

    def __init__(
        self,
        node_id: int = 1,
        *,
        aging_factor: float = DEFAULT_AGING_FACTOR,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        retry_base_s: float = DEFAULT_RETRY_BASE_S,
        retry_max_s: float = DEFAULT_RETRY_MAX_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if aging_factor <= 0:
            raise ValueError("aging_factor debe ser positivo")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds debe ser positivo")
        if retry_base_s <= 0 or retry_max_s < retry_base_s:
            raise ValueError("se requiere 0 < retry_base_s <= retry_max_s")
        self.node_id = node_id
        self.aging_factor = aging_factor
        self.ttl_seconds = ttl_seconds
        self.retry_base_s = retry_base_s
        self.retry_max_s = retry_max_s
        self._clock = clock
        self._counter: int = 0
        self._seq_counter: int = 0
        self._items: dict[int, QueueItem] = {}
        self._by_key: dict[tuple[MessageType, int, int], int] = {}

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter

    def _next_seq(self) -> int:
        self._seq_counter += 1
        return self._seq_counter

    def enqueue(
        self,
        payload: bytes,
        *,
        destination: str,
        urgency: Urgency = Urgency.MEDIA,
        msg_type: MessageType | None = None,
        device_id: int | None = None,
        seq: int | None = None,
        requires_ack: bool = True,
        ttl_seconds: float | None = None,
        base_weight: float | None = None,
    ) -> QueueItem:
        """Encola un mensaje para transmisión."""
        if not payload:
            raise ValueError("el payload no puede estar vacío")

        detected_type = msg_type
        detected_device = device_id
        detected_seq = seq

        if len(payload) >= HEADER_SIZE:
            try:
                hdr = decode(payload[:HEADER_SIZE])
                if detected_type is None:
                    detected_type = MessageType(hdr.msg_type)
                if detected_device is None:
                    detected_device = hdr.device_id
                if detected_seq is None:
                    detected_seq = hdr.seq
            except Exception:
                pass

        final_msg_type = detected_type if detected_type is not None else MessageType.REPORTE
        final_device_id = detected_device if detected_device is not None else self.node_id
        final_seq = detected_seq if detected_seq is not None else self._next_seq()

        # Determinar peso base
        if base_weight is not None:
            final_weight = float(base_weight)
        elif final_msg_type == MessageType.FRAGMENTO_FOTO:
            final_weight = PHOTO_WEIGHT
        elif final_msg_type in (
            MessageType.ACK,
            MessageType.A_SALVO,
            MessageType.SOS_ESCOMBROS,
        ):
            final_weight = CONTROL_WEIGHT
        else:
            final_weight = URGENCY_WEIGHTS.get(urgency, 40.0)

        # Si es un ACK saliente, no requiere ACK
        if final_msg_type == MessageType.ACK:
            requires_ack = False

        now = self._clock()
        item = QueueItem(
            id=self._next_id(),
            payload=payload,
            destination=destination,
            urgency=urgency,
            msg_type=final_msg_type,
            device_id=final_device_id,
            seq=final_seq,
            base_weight=final_weight,
            created_at=now,
            requires_ack=requires_ack,
            ttl_seconds=ttl_seconds if ttl_seconds is not None else self.ttl_seconds,
        )
        self._items[item.id] = item
        self._by_key[item.key] = item.id
        frag_type = (
            MessageType.FRAGMENTO_FOTO
            if final_msg_type == MessageType.FRAGMENTO_FOTO
            else MessageType.FRAGMENTO
        )
        self._by_key[(frag_type, item.device_id, item.seq)] = item.id
        return item

    def enqueue_ack(self, ack: Ack, *, destination: str) -> QueueItem:
        """Encola un ACK con prioridad de control inmediata."""
        from guadua.protocol.fragments import encode_ack

        return self.enqueue(
            encode_ack(ack),
            destination=destination,
            msg_type=MessageType.ACK,
            device_id=ack.device_id,
            seq=ack.seq,
            requires_ack=False,
            base_weight=CONTROL_WEIGHT,
        )

    def effective_priority(self, item: QueueItem, now: float | None = None) -> float:
        """Devuelve la prioridad efectiva actual de un elemento."""
        current_time = self._clock() if now is None else now
        return item.effective_priority(current_time, self.aging_factor)

    def max_wait_minutes(
        self, urgency: Urgency, target_urgency: Urgency = Urgency.CRITICA
    ) -> float:
        """Cálculo analítico de la espera máxima antes de superar a `target_urgency`."""
        diff = URGENCY_WEIGHTS[target_urgency] - URGENCY_WEIGHTS[urgency]
        return max(0.0, diff / self.aging_factor)

    def retry_wait(self, item: QueueItem) -> float:
        """Segundos que espera `item` desde su último envío antes de reenviarse: 0 si nunca
        salió; `retry_base_s` tras el primer envío y el doble en cada reintento, con tope."""
        if item.attempts == 0:
            return 0.0
        doublings = min(item.attempts - 1, 32)  # el tope llega mucho antes; evita desbordes
        return min(self.retry_base_s * 2**doublings, self.retry_max_s)

    def waiting_ack(self, item: QueueItem, now: float | None = None) -> bool:
        """¿Se envió hace menos de su espera y sigue sin ACK? Entonces no se reenvía aún."""
        if item.last_attempt_at is None:
            return False
        current_time = self._clock() if now is None else now
        return current_time - item.last_attempt_at < self.retry_wait(item)

    def check_timeouts(self, now: float | None = None) -> list[QueueItem]:
        """Pasa a 'por_confirmar' los mensajes que hayan superado su TTL."""
        current_time = self._clock() if now is None else now
        expired: list[QueueItem] = []
        for item in self._items.values():
            if (
                item.status in (ItemStatus.ENCOLADO, ItemStatus.TRANSMITIENDO)
                and current_time - item.created_at >= item.ttl_seconds
            ):
                item.status = ItemStatus.POR_CONFIRMAR
                expired.append(item)
        return expired

    def select_next(self, now: float | None = None) -> QueueItem | None:
        """Selecciona el siguiente elemento activo a transmitir según la política."""
        current_time = self._clock() if now is None else now
        self.check_timeouts(current_time)

        candidates = [
            item
            for item in self._items.values()
            if item.status in (ItemStatus.ENCOLADO, ItemStatus.TRANSMITIENDO)
            and (item.sender is None or not item.sender.done)
            and not self.waiting_ack(item, current_time)
        ]
        if not candidates:
            return None

        # Ordenar por:
        # 1. Mayor prioridad efectiva (-prioridad)
        # 2. Menor tamaño en bytes (len(payload))
        # 3. Más antiguo (created_at)
        # 4. ID secuencial
        def sort_key(it: QueueItem) -> tuple[float, int, float, int]:
            prio = it.effective_priority(current_time, self.aging_factor)
            return (-prio, len(it.payload), it.created_at, it.id)

        candidates.sort(key=sort_key)
        return candidates[0]

    def peek(self, now: float | None = None) -> QueueItem | None:
        """Inspecciona el siguiente elemento sin modificar nada."""
        return self.select_next(now)

    async def transmit_next(
        self, transport: Transport, now: float | None = None
    ) -> QueueItem | None:
        """Transmite el siguiente elemento a través del transporte."""
        current_time = self._clock() if now is None else now
        item = self.select_next(current_time)
        if item is None:
            return None

        item.attempts += 1
        item.last_attempt_at = current_time

        # Mensaje cabe en la MTU
        if len(item.payload) <= transport.mtu:
            await transport.send(item.destination, item.payload)
            if not item.requires_ack:
                item.status = ItemStatus.ENTREGADO
            else:
                item.status = ItemStatus.TRANSMITIENDO
            return item

        # Mensaje supera la MTU: requiere fragmentación
        if item.sender is None:
            frag_type = (
                MessageType.FRAGMENTO_FOTO
                if item.msg_type == MessageType.FRAGMENTO_FOTO
                else MessageType.FRAGMENTO
            )
            fragments = split(
                item.payload,
                device_id=item.device_id,
                seq=item.seq,
                mtu=transport.mtu,
                msg_type=frag_type,
            )
            item.sender = Sender(fragments)

        for fragment in item.sender.pending():
            await transport.send(item.destination, encode_fragment(fragment))

        item.status = ItemStatus.TRANSMITIENDO
        return item

    def on_ack(self, ack: Ack) -> bool:
        """Procesa un ACK confirmando fragmentos o entrega total."""
        item_id = self._by_key.get(ack.key)
        if item_id is None:
            for alt_type in (
                MessageType.FRAGMENTO,
                MessageType.FRAGMENTO_FOTO,
                MessageType.REPORTE,
            ):
                alt_key = (alt_type, ack.device_id, ack.seq)
                if alt_key in self._by_key:
                    item_id = self._by_key[alt_key]
                    break
        if item_id is None:
            return False

        item = self._items[item_id]
        if item.status in (ItemStatus.ENTREGADO, ItemStatus.POR_CONFIRMAR):
            return True

        if item.sender is not None:
            sender_type = item.sender._key[0]
            if ack.acked_type != sender_type:
                ack = Ack(sender_type, ack.device_id, ack.seq, ack.total, ack.received)
            item.sender.on_ack(ack)
            if item.sender.done:
                item.status = ItemStatus.ENTREGADO
                return True
        else:
            # Mensaje no fragmentado (total 1)
            if ack.complete:
                item.status = ItemStatus.ENTREGADO
                return True

        return True

    def process_incoming_frame(self, frame: bytes) -> bool:
        """Si la trama recibida es un ACK, la decodifica y procesa."""
        if len(frame) < 11:
            return False
        first_byte = frame[0]
        if (first_byte & 0x0F) == MessageType.ACK:
            try:
                ack = decode_ack(frame)
                return self.on_ack(ack)
            except Exception:
                return False
        return False

    def items(self, status: ItemStatus | None = None) -> list[QueueItem]:
        """Devuelve los elementos de la cola, opcionalmente filtrados por estado."""
        if status is None:
            return list(self._items.values())
        return [it for it in self._items.values() if it.status == status]

    def get_unconfirmed(self) -> list[QueueItem]:
        """Devuelve los reportes vencidos en estado 'por_confirmar'."""
        return self.items(ItemStatus.POR_CONFIRMAR)

    def stats(self, now: float | None = None) -> dict[str, Any]:
        """Resumen estadístico de la cola."""
        current_time = self._clock() if now is None else now
        self.check_timeouts(current_time)
        by_status: dict[str, int] = {}
        for it in self._items.values():
            by_status[it.status.value] = by_status.get(it.status.value, 0) + 1

        active = [
            it
            for it in self._items.values()
            if it.status in (ItemStatus.ENCOLADO, ItemStatus.TRANSMITIENDO)
        ]
        waits = [(current_time - it.created_at) / 60.0 for it in active]

        return {
            "total": len(self._items),
            "by_status": by_status,
            "active_count": len(active),
            "max_wait_minutes": max(waits) if waits else 0.0,
            "aging_factor": self.aging_factor,
        }

    def __len__(self) -> int:
        """Cantidad de elementos activos pendientes de entrega."""
        return len(
            [
                it
                for it in self._items.values()
                if it.status in (ItemStatus.ENCOLADO, ItemStatus.TRANSMITIENDO)
            ]
        )
