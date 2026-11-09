"""El nodo: junta registro de eventos, recepción, agentes y gossip. La API HTTP lo usa.

Todo corre en el mismo hilo del bucle de asyncio (las rutas son `async`), así que la
asignación de consecutivos y las escrituras no compiten entre sí.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from guadua import __version__
from guadua.agents.coordinacion import AgenteCoordinacion
from guadua.agents.correlacion import correlacionar_reporte
from guadua.classifier.triage import UrgencyClassifier, default_classifier
from guadua.node.api import DecisionAck, DecisionRequest, Health, MessageAck
from guadua.node.intake import parse_message, reporte_event
from guadua.node.pipeline import normalized_event, urgency_event
from guadua.providers.llm import LLMProvider
from guadua.providers.transport import Transport
from guadua.services.resilience import DEFAULT_PEER_TIMEOUT, Ownership, Resilience, Work
from guadua.store.event_types import Decision, UrgenciaEstimada, event_id_str, validate_event
from guadua.store.events import Event, EventCategory, EventId, EventStore
from guadua.store.gossip import GossipNode

if TYPE_CHECKING:
    from guadua.agents.triaje import AgenteTriaje, TriageCase

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
        llm: LLMProvider | None = None,
        peer_timeout: float = DEFAULT_PEER_TIMEOUT,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """`classifier="default"` entrena el de data/sinteticos (una vez por proceso);
        None deja solo las reglas. Sin `llm` no hay agente de triaje (N8): los casos dudosos
        quedan "ambiguo, revisar" con la urgencia de N7. `peer_timeout` y `monotonic` son de
        la resiliencia (N12): segundos sin latidos para dar un vecino por caído y su reloj."""
        self.node_id = node_id
        self.classifier = default_classifier() if classifier == "default" else classifier
        self.triaje = _agente_triaje(llm)
        self.agent_tasks: set[asyncio.Future] = set()  # ciclos del agente en curso
        self.triage_running: set[EventId] = set()  # urgencias que el agente está revisando
        self.store = store
        self.clock = clock
        self._seq = self._max_own_seq()
        known = store.since({})  # al reiniciar, el estado sale de la SQLite
        self._report_ids = {e.payload["report_id"] for e in known if e.type == "reporte"}
        self.ownership = Ownership(known)
        self.gossip = (
            GossipNode(
                node_id, store, transport, peers, on_event=self._after_append, monotonic=monotonic
            )
            if transport is not None
            else None
        )
        self.coordinacion = AgenteCoordinacion(node_id, self.next_seq)
        self.resilience = Resilience(
            self, timeout=peer_timeout, monotonic=monotonic, restarted=bool(known)
        )

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
        self.ownership.add(event)  # una reasignación cambia de quién es el trabajo
        try:
            self._run_agents(event)
        except Exception:  # un agente nunca tumba el nodo ni el ACK ya dado
            logger.exception("%s: falló un agente con el evento %s:%s", self.node_id, *event.id)

    def owns(self, event_id: EventId) -> bool:
        """¿Es de este nodo el trabajo que dispara el evento? Sin reasignaciones, lo es si
        el evento es suyo (diseño §9: solo el dueño deriva). Ver services/resilience.py."""
        return self.ownership.owner_of(event_id) == self.node_id

    def _run_agents(self, event: Event) -> None:
        if event.type == "decision":
            ficha = self.store.get(event.refs[0]) if event.refs else None
            if ficha is not None and self.owns(ficha.id):
                self.coordinacion.procesar_decision(event, self.store)  # replanea si rechazan
        elif self.owns(event.id):
            match event.type:
                case "reporte":
                    self._normalize(event)
                case "reporte_normalizado":
                    self._estimate_urgency(event)
                    self._group(event)
                case "urgencia_estimada":
                    self._start_triage(event)
                case "incidente_agrupado":
                    self.coordinacion.procesar_incidente(event, self.store)

    def do_work(self, work: Work) -> None:
        """Un paso pendiente que la resiliencia (N12) encontró en el registro: el de un nodo
        caído que este tomó, o uno propio que se cortó (p. ej., el triaje al caer el nodo).
        Cada paso revisa el registro antes de actuar, así que repetirlo no duplica."""
        match work.kind:
            case "normalizar":
                self._normalize(work.event)
            case "urgencia":
                self._estimate_urgency(work.event)
            case "agrupar":
                self._group(work.event)
            case "triaje":
                self._start_triage(work.event)
            case "ficha":
                self.coordinacion.procesar_incidente(work.event, self.store)
            case "replanear":
                self.coordinacion.procesar_decision(work.event, self.store)

    def _has(self, event_type: str, report_id: str) -> bool:
        return any(
            e.type == event_type and e.payload.get("report_id") == report_id
            for e in self.store.since({})
        )

    def _normalize(self, reporte: Event) -> None:
        if not self._has("reporte_normalizado", reporte.payload["report_id"]):
            self.record(normalized_event(reporte, node_id=self.node_id, seq=self.next_seq()))

    def _group(self, normalized: Event) -> None:
        """Correlación (N9). El agente ya revisa si el reporte está agrupado."""
        grouped = correlacionar_reporte(normalized, self.store, self.node_id, self.next_seq)
        if grouped is not None:
            self._after_append(grouped)  # el agente ya lo guardó

    def _estimate_urgency(self, normalized: Event) -> None:
        """Publica `urgencia_estimada`. Si falla, la correlación y el resto siguen igual."""
        if self._has("urgencia_estimada", normalized.payload["report_id"]):
            return
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

    def _start_triage(self, urgency: Event) -> None:
        """Agente de triaje (N8) sobre una urgencia ambigua de N7.

        El ciclo llama al LLM y puede tardar (Ollama en CPU), así que corre en otro hilo:
        el ACK y la correlación no lo esperan. El caso se arma aquí, con una copia del
        registro, y el resultado se guarda de vuelta en el hilo del nodo. Sin bucle de
        asyncio (pruebas, scripts) corre en línea. Una falla nunca tumba el nodo.
        """
        if self.triaje is None:
            return
        case = self.triaje.prepare(urgency, self.store)
        if case is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._publish_triage(case, self.triaje.run(case))
            return
        future = loop.run_in_executor(None, self.triaje.run, case)
        self.agent_tasks.add(future)
        self.triage_running.add(urgency.id)

        def done(f: asyncio.Future) -> None:
            self.agent_tasks.discard(f)
            self.triage_running.discard(urgency.id)
            if f.cancelled():
                return
            if (exc := f.exception()) is not None:
                logger.error("%s: falló el agente de triaje: %r", self.node_id, exc)
                return
            self._publish_triage(case, f.result())

        future.add_done_callback(done)

    def _publish_triage(self, case: TriageCase, payload: UrgenciaEstimada) -> None:
        urgency = case.urgency_event
        if not self.owns(urgency.id) or any(
            e.type == "urgencia_estimada" and urgency.id in e.refs for e in self.store.since({})
        ):  # mientras el agente pensaba, otro nodo tomó el caso o ya lo revisó
            logger.warning("%s: el triaje de %s:%s ya no le toca", self.node_id, *urgency.id)
            return
        try:
            self.record(self.triaje.event(case, payload, node_id=self.node_id, seq=self.next_seq()))
        except Exception:
            logger.exception("%s: no se pudo publicar el triaje del agente", self.node_id)


def _agente_triaje(llm: LLMProvider | None) -> AgenteTriaje | None:
    if llm is None:
        return None
    try:
        from guadua.agents.triaje import AgenteTriaje
    except ImportError:
        logger.warning("sin LangGraph (extra agents): no hay agente de triaje")
        return None
    return AgenteTriaje(llm)
