"""El nodo: junta registro de eventos, recepción, agentes y gossip. La API HTTP lo usa.

Todo corre en el mismo hilo del bucle de asyncio (las rutas son `async`), así que la
asignación de consecutivos y las escrituras no compiten entre sí.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Literal

from guadua import __version__
from guadua.agents.coordinacion import AgenteCoordinacion
from guadua.agents.correlacion import correlacionar_reporte
from guadua.classifier.triage import UrgencyClassifier, default_classifier
from guadua.node.api import DecisionAck, DecisionRequest, Health, MessageAck
from guadua.node.intake import parse_message, reporte_event
from guadua.node.pipeline import normalized_event, urgency_event
from guadua.providers.transport import Transport
from guadua.store.event_types import Decision, event_id_str, validate_event
from guadua.store.events import Event, EventCategory, EventId, EventStore
from guadua.store.gossip import GossipNode

logger = logging.getLogger("guadua.node")


class NotFound(LookupError):
    """→ 404."""


class Conflict(RuntimeError):
    """→ 409."""


def parse_event_id(text: str) -> EventId:
    origin, seq = text.split(":", 1)
    return origin, int(seq)


class Node:
    def __init__(
        self,
        node_id: str,
        store: EventStore,
        *,
        transport: Transport | None = None,
        peers: tuple[str, ...] = (),
        clock: Callable[[], float] = time.time,
        classifier: UrgencyClassifier | None | Literal["default"] = "default",
    ) -> None:
        """`classifier="default"` entrena el de data/sinteticos (una vez por proceso);
        None deja solo las reglas."""
        self.node_id = node_id
        self.classifier = default_classifier() if classifier == "default" else classifier
        self.store = store
        self.clock = clock
        self._seq = self._max_own_seq()
        self._report_ids = {e.payload["report_id"] for e in store.since({}) if e.type == "reporte"}
        self.gossip = (
            GossipNode(node_id, store, transport, peers, on_event=self._after_append)
            if transport is not None
            else None
        )
        self.coordinacion = AgenteCoordinacion(node_id, self.next_seq)

    # --- Consecutivos ----------------------------------------------------------------------

    def _max_own_seq(self) -> int:
        last_seq = getattr(self.store, "last_seq", None)
        if callable(last_seq):
            return last_seq(self.node_id)
        return max(
            (e.seq for e in self.store.since({}) if e.origin_node == self.node_id), default=0
        )

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # --- Rutas -----------------------------------------------------------------------------

    def health(self) -> Health:
        alive = self.gossip.peers_alive() if self.gossip else ()
        return Health(node=self.node_id, version=__version__, peers_alive=alive)

    def receive_message(self, data: bytes) -> MessageAck:
        """Bytes de la PWA → evento `reporte`. Lanza IntakeError (→ 400) si no sirven."""
        message = parse_message(data)
        if message.report_id in self._report_ids:
            return MessageAck(report_id=message.report_id, duplicate=True)
        event = reporte_event(
            message, node_id=self.node_id, seq=self.next_seq(), received_at=self.clock()
        )
        self.record(event)
        return MessageAck(report_id=message.report_id)

    def decide(self, request: DecisionRequest) -> DecisionAck:
        ficha = self.store.get(parse_event_id(request.ficha_id))
        if ficha is None or ficha.type != "ficha":
            raise NotFound(f"no existe la ficha {request.ficha_id}")
        if any(e.type == "decision" and e.refs[:1] == (ficha.id,) for e in self.store.since({})):
            raise Conflict(f"la ficha {request.ficha_id} ya tiene una decisión")
        payload = Decision(
            action=request.action, edited_message=request.edited_message, operator=request.operator
        )
        event = Event(
            origin_node=self.node_id,
            seq=self.next_seq(),
            category=EventCategory.DECISION,
            type="decision",
            payload=payload.model_dump(mode="json"),
            refs=(ficha.id,),
            received_at=self.clock(),
        )
        self.record(event)
        return DecisionAck(event_id=event_id_str(event))

    # --- Registro y agentes ----------------------------------------------------------------

    def record(self, event: Event) -> bool:
        """Valida, guarda y dispara lo que corresponda. Lo usan la API y el propio nodo."""
        validate_event(event)
        if not self.store.append(event):
            return False
        self._after_append(event)
        return True

    def _after_append(self, event: Event) -> None:
        """Se llama con cada evento nuevo, venga de la API o del gossip."""
        if event.type == "reporte":
            self._report_ids.add(event.payload["report_id"])
        try:
            self._run_agents(event)
        except Exception:  # un agente nunca tumba el nodo ni el ACK ya dado
            logger.exception("%s: falló un agente con el evento %s:%s", self.node_id, *event.id)

    def _run_agents(self, event: Event) -> None:
        if event.type == "reporte" and event.origin_node == self.node_id:
            normalized = normalized_event(event, node_id=self.node_id, seq=self.next_seq())
            self.record(normalized)
        elif event.type == "reporte_normalizado" and event.origin_node == self.node_id:
            self._estimate_urgency(event)
            grouped = correlacionar_reporte(event, self.store, self.node_id, self.next_seq)
            if grouped is not None:
                self._after_append(grouped)  # el agente ya lo guardó
        elif event.type == "incidente_agrupado" and event.origin_node == self.node_id:
            self.coordinacion.procesar_incidente(event, self.store)
        elif event.type == "decision":
            ficha = self.store.get(event.refs[0]) if event.refs else None
            if ficha is not None and ficha.origin_node == self.node_id:
                self.coordinacion.procesar_decision(event, self.store)  # replanea si rechazan

    def _estimate_urgency(self, normalized: Event) -> None:
        """Publica `urgencia_estimada`. Si falla, la correlación y el resto siguen igual."""
        try:
            reporte = self.store.get(normalized.refs[0])
            if reporte is None or reporte.type != "reporte":
                return
            self.record(
                urgency_event(
                    normalized, reporte, self.classifier, node_id=self.node_id, seq=self.next_seq()
                )
            )
        except Exception:
            logger.exception("%s: falló el triaje del evento %s:%s", self.node_id, *normalized.id)
