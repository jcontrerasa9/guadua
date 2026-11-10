"""Dobles de prueba de las interfaces. Sin red ni API key."""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from collections.abc import Sequence

from guadua.knowledge.retriever import Passage
from guadua.providers.llm import LLMReply, LLMUnavailable, Message, ToolSpec
from guadua.providers.transcriber import TranscriberUnavailable, Transcription
from guadua.store.events import Event, EventId, VersionVector


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: dict[EventId, Event] = {}

    def append(self, event: Event) -> bool:
        if event.id in self._events:
            return False
        self._events[event.id] = event
        return True

    def get(self, event_id: EventId) -> Event | None:
        return self._events.get(event_id)

    def since(self, vector: VersionVector) -> list[Event]:
        return sorted(
            (e for e in self._events.values() if e.seq > vector.get(e.origin_node, 0)),
            key=lambda e: e.id,
        )

    def version_vector(self) -> VersionVector:
        vector: VersionVector = {}
        for origin, seq in sorted(self._events):
            if seq == vector.get(origin, 0) + 1:
                vector[origin] = seq
        return vector

    def digest(self) -> str:
        sha = hashlib.sha256()
        for event_id in sorted(self._events):
            sha.update(self._events[event_id].model_dump_json().encode())
        return sha.hexdigest()


class NullLLM:
    """Simula "sin LLM": siempre falla, y el caso debe ir a revisión humana."""

    name = "null"

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> LLMReply:
        raise LLMUnavailable("sin proveedor de LLM")


class ScriptedLLM:
    """Devuelve respuestas fijas en orden (pruebas deterministas)."""

    name = "scripted"

    def __init__(self, replies: Sequence[LLMReply]) -> None:
        self._replies = deque(replies)

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> LLMReply:
        if not self._replies:
            raise LLMUnavailable("guion agotado")
        return self._replies.popleft()


class FakeTranscriber:
    """Transcriptor simulado (D7): devuelve el texto fijado para cada audio, sin Whisper.
    Un audio que no conoce no se entiende (TranscriberUnavailable)."""

    name = "fake"

    def __init__(self, texts: dict[bytes, str] | None = None) -> None:
        self.texts = dict(texts or {})
        self.calls = 0

    def transcribe(self, audio: bytes) -> Transcription:
        self.calls += 1
        if audio not in self.texts:
            raise TranscriberUnavailable("audio ininteligible")
        return Transcription(self.texts[audio], "es", 9.5, 0.8, "fake/whisper")


class StaticRetriever:
    def __init__(self, passages: Sequence[Passage]) -> None:
        self._passages = list(passages)

    def search(self, query: str, k: int = 3) -> list[Passage]:
        return sorted(self._passages, key=lambda p: p.score, reverse=True)[:k]


class LoopbackTransport:
    def __init__(self, mtu: int = 140) -> None:
        self.mtu = mtu
        self._queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()

    async def send(self, destination: str, frame: bytes) -> None:
        if len(frame) > self.mtu:
            raise ValueError(f"trama de {len(frame)} B supera la MTU de {self.mtu} B")
        await self._queue.put((destination, frame))

    async def receive(self, timeout: float) -> tuple[str, bytes] | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None
