"""El nodo: junta registro de eventos, recepción, agentes y gossip. La API HTTP lo usa.

Todo corre en el mismo hilo del bucle de asyncio (las rutas son `async`), así que la
asignación de consecutivos y las escrituras no compiten entre sí.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from guadua import __version__
from guadua.agents.coordinacion import AgenteCoordinacion
from guadua.agents.correlacion import (
    correlacionar_confirmacion_campo,
    correlacionar_reporte,
)
from guadua.agents.recepcion import DEFAULT_KINDS, AgenteRecepcion, QuestionCase
from guadua.agents.recepcion_radio import (
    SeparadorRadio,
    Split,
    SplitCase,
    current_transcriptions,
    split_done,
    superseded,
)
from guadua.agents.vigia import (
    DEFAULT_UMBRAL_CRITICO_S,
    DEFAULT_UMBRAL_OLVIDO_S,
    AgenteVigia,
    solicitud_event,
)
from guadua.classifier.triage import UrgencyClassifier, default_classifier
from guadua.knowledge.retriever import Retriever
from guadua.node.api import (
    DecisionAck,
    DecisionRequest,
    Digest,
    Health,
    MessageAck,
    RadioAck,
    TranscriptionRequest,
)
from guadua.node.intake import (
    IntakeError,
    ParsedAnswer,
    a_salvo_event,
    check_answer,
    parse_message,
    reporte_event,
    respuesta_event,
)
from guadua.node.pipeline import normalized_event, urgency_event
from guadua.protocol.header import MessageType
from guadua.providers.llm import LLMProvider
from guadua.providers.transcriber import NullTranscriber, Transcriber, TranscriberUnavailable
from guadua.providers.transport import Transport
from guadua.radio.clips import Clip, ClipStore, MemoryClipStore, audio_id_of, check_audio
from guadua.radio.texto import any_direct_cue
from guadua.services.resilience import DEFAULT_PEER_TIMEOUT, Ownership, Resilience, Work
from guadua.store.event_types import (
    Decision,
    PreguntaSeguimiento,
    QuestionKind,
    TranscripcionRadio,
    UrgenciaEstimada,
    event_id_str,
    validate_event,
)
from guadua.store.events import Event, EventCategory, EventId, EventStore
from guadua.store.gossip import GossipNode

if TYPE_CHECKING:
    from guadua.agents.triaje import AgenteTriaje, TriageCase

logger = logging.getLogger("guadua.node")

# Eventos que nacen de un mensaje de la PWA con su report_id (para detectar repetidos).
MESSAGE_EVENTS = frozenset({"reporte", "a_salvo"})


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
        recepcion_llm: LLMProvider | None = None,
        question_kinds: frozenset[QuestionKind] = DEFAULT_KINDS,
        transcriber: Transcriber | None = None,
        clips: ClipStore | None = None,
        retriever: Retriever | None | Literal["default"] = "default",
        vigia_critico_s: float = DEFAULT_UMBRAL_CRITICO_S,
        vigia_olvido_s: float = DEFAULT_UMBRAL_OLVIDO_S,
    ) -> None:
        """`classifier="default"` entrena el de data/sinteticos (una vez por proceso);
        None deja solo las reglas. Sin `llm` no hay agente de triaje (N8): los casos dudosos
        quedan "ambiguo, revisar" con la urgencia de N7. `peer_timeout` y `monotonic` son de
        la resiliencia (N12): segundos sin latidos para dar un vecino por caído y su reloj.
        `recepcion_llm` es el del agente de recepción (D5a); sin él, las preguntas de
        seguimiento salen de las reglas. `question_kinds`: los tipos de pregunta que la PWA
        sabe mostrar. `transcriber` y `clips` son de la radio (D7): sin transcriptor, los
        clips quedan pendientes para el operador; el audio se guarda en `clips` (en memoria
        si no se da). La separación de la radio usa el mismo LLM que la recepción.
        `retriever` es el RAG de la coordinación (D8): `"default"` indexa
        data/emre_passages.json (una vez por proceso); None deja solo la matriz EMRE.
        `vigia_critico_s` y `vigia_olvido_s` son los umbrales del vigía (D6): segundos sin
        atender para un caso crítico o alto, y para los demás."""
        self.node_id = node_id
        self.powered = True  # False: apagado simulado desde la barra del jurado (D9)
        self.classifier = default_classifier() if classifier == "default" else classifier
        self.triaje = _agente_triaje(llm)
        self.agent_tasks: set[asyncio.Future] = set()  # ciclos del agente en curso
        self.triage_running: set[EventId] = set()  # urgencias que el agente está revisando
        self.recepcion = AgenteRecepcion(recepcion_llm, kinds=question_kinds)
        self.asking: set[EventId] = set()  # reportes normalizados con la pregunta en curso
        self.transcriber = transcriber or NullTranscriber()
        self.clips = clips if clips is not None else MemoryClipStore()
        self.radio = SeparadorRadio(recepcion_llm)
        self.transcribing: set[str] = set()  # audio_id con Whisper trabajando
        self.splitting: set[EventId] = set()  # transcripciones que el agente está separando
        self.radio_notes: dict[str, str] = {}  # audio_id → por qué quedó pendiente
        self.store = store
        self.clock = clock
        self._seq = self._max_own_seq()
        known = store.since({})  # al reiniciar, el estado sale de la SQLite
        self._report_ids = {e.payload["report_id"] for e in known if e.type in MESSAGE_EVENTS}
        self.ownership = Ownership(known)
        self.gossip = (
            GossipNode(
                node_id, store, transport, peers, on_event=self._after_append, monotonic=monotonic
            )
            if transport is not None
            else None
        )
        self.coordinacion = AgenteCoordinacion(
            node_id,
            self.next_seq,
            retriever=default_retriever() if retriever == "default" else retriever,
        )
        self.vigia = AgenteVigia(
            store, clock=clock, umbral_olvido_s=vigia_olvido_s, umbral_critico_s=vigia_critico_s
        )
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

    def digest(self) -> Digest:
        """Huella del registro: si coincide en los tres nodos, convergieron (D9)."""
        return Digest(
            node=self.node_id, digest=self.store.digest(), events=len(self.store.since({}))
        )

    def set_power(self, on: bool) -> None:
        """Apagado simulado (D9). Apagado, la API responde 503 y el gossip calla, así que
        los vecinos lo dan por caído y toman su trabajo (N12). Al encenderlo se pone al día
        desde su registro y el gossip, como al reiniciar."""
        if on == self.powered:
            return
        self.powered = on
        if self.gossip is not None:
            self.gossip.muted = not on
        if on:
            self.resilience.resume()
        logger.warning("%s: %s (simulación)", self.node_id, "encendido" if on else "apagado")

    def receive_message(self, data: bytes) -> MessageAck:
        """Bytes de la PWA → evento `reporte` o `respuesta_seguimiento`. Lanza IntakeError
        (→ 400) si no sirven."""
        message = parse_message(data)
        if isinstance(message, ParsedAnswer):
            return self._receive_answer(message)
        if message.report_id in self._report_ids:
            return MessageAck(report_id=message.report_id, duplicate=True)
        build = a_salvo_event if message.header.msg_type == MessageType.A_SALVO else reporte_event
        event = build(message, node_id=self.node_id, seq=self.next_seq(), received_at=self.clock())
        self.record(event)
        return MessageAck(report_id=message.report_id)

    def _receive_answer(self, message: ParsedAnswer) -> MessageAck:
        """Vale la primera respuesta de cada pregunta; las demás son duplicadas. Si el nodo
        aún no conoce la pregunta (no le llegó por gossip), la acepta igual."""
        qid = message.answer.question_id
        if any(
            e.type == "respuesta_seguimiento" and e.payload["question_id"] == qid
            for e in self.store.since({})
        ):
            return MessageAck(report_id=message.report_id, duplicate=True)
        question = self.store.get(parse_event_id(qid))
        if question is not None:
            if question.type != "pregunta_seguimiento":
                raise IntakeError(f"{qid} no es una pregunta de seguimiento")
            check_answer(PreguntaSeguimiento.model_validate(question.payload), message)
        event = respuesta_event(
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

    # --- Radio VHF (D7) ---------------------------------------------------------------------

    async def receive_audio(
        self, data: bytes, content_type: str | None, *, name: str | None = None
    ) -> RadioAck:
        """Guarda el clip, lo transcribe en otro hilo y publica `transcripcion_radio`. Lanza
        ClipError (→ 400) si el audio no sirve. Sin transcriptor o si Whisper no puede, el
        clip queda pendiente para el operador; el mismo clip otra vez se vuelve a intentar."""
        kind = check_audio(data, content_type)
        audio_id = audio_id_of(data)
        current = current_transcriptions(self.store.since({})).get(audio_id)
        if current is not None:
            return self._radio_ack(current, duplicate=True)
        self.clips.put(Clip(audio_id, kind, self.clock(), len(data), name), data)
        if audio_id in self.transcribing:
            return RadioAck(audio_id=audio_id, status="pendiente", note="transcribiendo")
        self.transcribing.add(audio_id)
        try:
            result = await asyncio.to_thread(self.transcriber.transcribe, data)
        except TranscriberUnavailable as exc:
            return self._pending(audio_id, str(exc))
        except Exception as exc:  # el transcriptor nunca tumba el nodo
            logger.exception("%s: falló el transcriptor con %s", self.node_id, audio_id)
            return self._pending(audio_id, f"falló el transcriptor: {type(exc).__name__}")
        finally:
            self.transcribing.discard(audio_id)
        if not result.text.strip():
            return self._pending(audio_id, "Whisper no reconoció palabras")
        current = current_transcriptions(self.store.since({})).get(audio_id)
        if current is not None:  # mientras transcribía, el operador escribió el texto
            return self._radio_ack(current, duplicate=True)
        payload = TranscripcionRadio(
            audio_id=audio_id,
            text=result.text,
            direct=any_direct_cue(result.text),
            source="whisper",
            model=result.model,
            duration_s=result.duration_s,
            confidence=result.confidence,
        )
        return self._radio_ack(self._publish_transcription(payload, refs=()))

    def correct_transcription(self, audio_id: str, request: TranscriptionRequest) -> RadioAck:
        """El operador escribe el texto de un clip pendiente o corrige el vigente: un evento
        nuevo con el anterior en `refs[0]` (los eventos no se editan). 404 si no lo conoce."""
        current = current_transcriptions(self.store.since({})).get(audio_id)
        if current is None and self.clips.get(audio_id) is None:
            raise NotFound(f"no existe el clip {audio_id}")
        text = " ".join(request.text.split())
        payload = TranscripcionRadio(
            audio_id=audio_id,
            text=text,
            direct=any_direct_cue(text),
            source="operador",
            operator=request.operator,
        )
        refs = (current.id,) if current is not None else ()
        self.radio_notes.pop(audio_id, None)
        return self._radio_ack(self._publish_transcription(payload, refs=refs))

    def _publish_transcription(self, payload: TranscripcionRadio, *, refs: tuple) -> Event:
        event = Event(
            origin_node=self.node_id,
            seq=self.next_seq(),
            category=EventCategory.HECHO,
            type="transcripcion_radio",
            payload=payload.model_dump(mode="json"),
            refs=refs,
            received_at=self.clock(),
        )
        self.record(event)
        return event

    def _pending(self, audio_id: str, why: str) -> RadioAck:
        logger.warning("%s: el clip %s queda pendiente (%s)", self.node_id, audio_id, why)
        self.radio_notes[audio_id] = why
        return RadioAck(audio_id=audio_id, status="pendiente", note=why)

    @staticmethod
    def _radio_ack(event: Event, *, duplicate: bool = False) -> RadioAck:
        return RadioAck(
            audio_id=event.payload["audio_id"],
            status="transcrito",
            transcription_id=event_id_str(event),
            text=event.payload["text"],
            duplicate=duplicate,
        )

    def _separate(self, transcription: Event) -> None:
        """Agente de recepción (D7): separa la transcripción en reportes, uno por suceso.

        Con LLM (solo si las reglas ven varios sucesos) corre en otro hilo, como las
        preguntas; si no, en línea. Una falla nunca tumba el nodo."""
        if transcription.id in self.splitting:
            return
        try:
            case = self.radio.prepare(transcription, self.store)
        except Exception:
            logger.exception("%s: falló la separación de %s:%s", self.node_id, *transcription.id)
            return
        if case is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or not self.radio.needs_llm(case):
            self._publish_split(case, self.radio.run(case))
            return
        future = loop.run_in_executor(None, self.radio.run, case)
        self.agent_tasks.add(future)
        self.splitting.add(transcription.id)

        def done(f: asyncio.Future) -> None:
            self.agent_tasks.discard(f)
            self.splitting.discard(transcription.id)
            if f.cancelled():
                return
            if (exc := f.exception()) is not None:
                logger.error("%s: falló la separación de la radio: %r", self.node_id, exc)
                return
            self._publish_split(case, f.result())

        future.add_done_callback(done)

    def _publish_split(self, case: SplitCase, split: Split) -> None:
        source = case.transcription_event
        events = self.store.since({})
        if (
            not self.owns(source.id)
            or split_done(events, source.id)
            or superseded(events, source.id)
        ):
            logger.warning("%s: la separación de %s:%s ya no le toca", self.node_id, *source.id)
            return
        seqs = [self.next_seq() for _ in split.parts]
        try:
            for event in self.radio.events(case, split, node_id=self.node_id, seqs=seqs):
                self.record(event)
        except Exception:
            logger.exception("%s: no se pudieron publicar los reportes de radio", self.node_id)

    # --- Vigía (D6) --------------------------------------------------------------------------

    def watch(self, now: float | None = None) -> list[Event]:
        """Una ronda del vigía: publica una `solicitud_vigia` por cada caso olvidado que sea
        de este nodo (el dueño de su `incidente_agrupado` más reciente), como máximo una por
        incidente y acción. Nunca cambia urgencias. Apagado (D9) no hace nada. Nunca lanza."""
        if not self.powered:
            return []
        try:
            return self._watch(now)
        except Exception:
            logger.exception("%s: falló la ronda del vigía", self.node_id)
            return []

    def _watch(self, now: float | None) -> list[Event]:
        events = self.store.since({})
        incidents: dict[str, Event] = {}
        for e in events:
            if e.type == "incidente_agrupado":
                iid = e.payload["incident_id"]
                known = incidents.get(iid)
                if known is None or (e.received_at, e.id) > (known.received_at, known.id):
                    incidents[iid] = e
        done = {
            (e.payload["incident_id"], e.payload["action"])
            for e in events
            if e.type == "solicitud_vigia"
        }
        published: list[Event] = []
        for request in self.vigia.revisar_casos_olvidados(now):
            anchor = incidents.get(request.incident_id)
            key = (request.incident_id, request.action)
            if anchor is None or key in done or not self.owns(anchor.id):
                continue
            done.add(key)
            event = solicitud_event(
                request, anchor, node_id=self.node_id, seq=self.next_seq(), received_at=self.clock()
            )
            if self.record(event):
                published.append(event)
                logger.info("%s: vigía → %s", self.node_id, request.reason)
        return published

    async def run_vigia(self, interval: float = 30.0, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            self.watch()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)

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
        if event.type in MESSAGE_EVENTS:
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
                    self._ask(event)
                case "urgencia_estimada":
                    self._start_triage(event)
                case "incidente_agrupado":
                    self.coordinacion.procesar_incidente(event, self.store)
                case "confirmacion_campo":
                    self._process_confirmacion_campo(event)
                case "transcripcion_radio":
                    self._separate(event)

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
            case "preguntar":
                self._ask(work.event)
            case "ficha":
                self.coordinacion.procesar_incidente(work.event, self.store)
            case "replanear":
                self.coordinacion.procesar_decision(work.event, self.store)
            case "confirmacion_campo":
                self._process_confirmacion_campo(work.event)
            case "separar":
                self._separate(work.event)

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

    def _process_confirmacion_campo(self, confirmacion: Event) -> None:
        """Correlación (D4). Actualiza confianza y contradicciones si el campo confirma
        o descarta."""
        grouped = correlacionar_confirmacion_campo(
            confirmacion, self.store, self.node_id, self.next_seq
        )
        if grouped is not None:
            self._after_append(grouped)

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

    def _ask(self, normalized: Event) -> None:
        """Agente de recepción (D5a): como máximo una pregunta de seguimiento por reporte.

        Con LLM corre en otro hilo, como el triaje; sin bucle de asyncio o sin LLM, en línea.
        Una falla nunca tumba el nodo: la pregunta es una ayuda, no un paso obligatorio.
        """
        if normalized.id in self.asking:
            return
        try:
            case = self.recepcion.prepare(normalized, self.store)
        except Exception:
            logger.exception("%s: falló la recepción de %s:%s", self.node_id, *normalized.id)
            return
        if case is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or not self.recepcion.needs_llm(case):
            self._publish_question(case, self.recepcion.run(case))
            return
        future = loop.run_in_executor(None, self.recepcion.run, case)
        self.agent_tasks.add(future)
        self.asking.add(normalized.id)

        def done(f: asyncio.Future) -> None:
            self.agent_tasks.discard(f)
            self.asking.discard(normalized.id)
            if f.cancelled():
                return
            if (exc := f.exception()) is not None:
                logger.error("%s: falló el agente de recepción: %r", self.node_id, exc)
                return
            self._publish_question(case, f.result())

        future.add_done_callback(done)

    def _publish_question(self, case: QuestionCase, payload: PreguntaSeguimiento | None) -> None:
        if payload is None:
            return
        normalized = case.normalized_event
        if not self.owns(normalized.id) or self._has("pregunta_seguimiento", case.report_id):
            logger.warning("%s: la pregunta de %s ya no le toca", self.node_id, case.report_id)
            return  # máximo una por reporte, aunque el LLM haya tardado
        try:
            self.record(
                self.recepcion.event(case, payload, node_id=self.node_id, seq=self.next_seq())
            )
        except Exception:
            logger.exception("%s: no se pudo publicar la pregunta", self.node_id)

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


@functools.cache
def default_retriever() -> Retriever | None:
    """RAG de la coordinación (D8) con el corpus de data/. Si no carga, solo la matriz EMRE."""
    try:
        from guadua.knowledge.hybrid_retriever import HybridRetriever

        return HybridRetriever.default()
    except Exception:
        logger.exception("no se pudo cargar el RAG: las fichas citan solo la matriz EMRE")
        return None


def _agente_triaje(llm: LLMProvider | None) -> AgenteTriaje | None:
    if llm is None:
        return None
    try:
        from guadua.agents.triaje import AgenteTriaje
    except ImportError:
        logger.warning("sin LangGraph (extra agents): no hay agente de triaje")
        return None
    return AgenteTriaje(llm)
