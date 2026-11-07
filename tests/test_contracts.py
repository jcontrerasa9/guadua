import asyncio

import pytest
from pydantic import ValidationError

from sard.classifier.urgency import Urgency, apply_floor
from sard.knowledge.retriever import Passage, Retriever
from sard.providers.llm import LLMProvider, LLMReply, LLMUnavailable, Message
from sard.providers.transport import Transport
from sard.store.events import Event, EventCategory, EventStore
from tests.fakes import (
    InMemoryEventStore,
    LoopbackTransport,
    NullLLM,
    ScriptedLLM,
    StaticRetriever,
)


def make_event(origin: str = "A", seq: int = 1, **kw) -> Event:
    return Event(
        origin_node=origin,
        seq=seq,
        category=kw.pop("category", EventCategory.HECHO),
        type=kw.pop("type", "reporte"),
        received_at=kw.pop("received_at", 0.0),
        **kw,
    )


# --- Urgencia ---------------------------------------------------------------


@pytest.mark.parametrize("floor", list(Urgency))
@pytest.mark.parametrize("proposed", list(Urgency))
def test_floor_never_goes_down(floor: Urgency, proposed: Urgency) -> None:
    result = apply_floor(floor, proposed)
    assert result >= floor
    assert result == max(floor, proposed)


# --- Eventos ----------------------------------------------------------------


def test_event_is_immutable() -> None:
    event = make_event()
    with pytest.raises(ValidationError):
        event.seq = 2  # type: ignore[misc]


def test_event_id_is_origin_and_seq() -> None:
    assert make_event("B", 8).id == ("B", 8)


def test_event_rejects_unknown_fields_and_bad_seq() -> None:
    with pytest.raises(ValidationError):
        make_event(extra="x")
    with pytest.raises(ValidationError):
        make_event(seq=0)


# --- Almacenamiento: cada implementación real se agrega a STORES -------------

STORES = [InMemoryEventStore]


@pytest.fixture(params=STORES, ids=lambda cls: cls.__name__)
def store(request) -> EventStore:
    return request.param()


def test_store_satisfies_interface(store: EventStore) -> None:
    assert isinstance(store, EventStore)


def test_store_append_is_idempotent(store: EventStore) -> None:
    event = make_event()
    assert store.append(event) is True
    assert store.append(event) is False
    assert store.get(event.id) == event


def test_store_version_vector_and_since(store: EventStore) -> None:
    for origin, seq in [("A", 1), ("A", 2), ("B", 1), ("A", 4)]:
        store.append(make_event(origin, seq))
    assert store.version_vector() == {"A": 2, "B": 1}
    assert [e.id for e in store.since({"A": 1})] == [("A", 2), ("A", 4), ("B", 1)]


def test_store_digest_ignores_arrival_order() -> None:
    events = [make_event("A", 1), make_event("B", 1), make_event("A", 2)]
    first, second = InMemoryEventStore(), InMemoryEventStore()
    for e in events:
        first.append(e)
    for e in reversed(events):
        second.append(e)
    assert first.digest() == second.digest()


# --- Proveedores ------------------------------------------------------------


def test_llm_fakes_satisfy_interface() -> None:
    assert isinstance(NullLLM(), LLMProvider)
    assert isinstance(ScriptedLLM([]), LLMProvider)


def test_without_llm_raises_unavailable() -> None:
    with pytest.raises(LLMUnavailable):
        NullLLM().complete([Message("user", "hola")], [])


def test_scripted_llm_replies_in_order() -> None:
    llm = ScriptedLLM([LLMReply(tool_name="clasificar"), LLMReply(final={"urgencia": "alta"})])
    assert llm.complete([], []).tool_name == "clasificar"
    assert llm.complete([], []).final == {"urgencia": "alta"}
    with pytest.raises(LLMUnavailable):
        llm.complete([], [])


def test_retriever_returns_top_k_with_citation() -> None:
    retriever = StaticRetriever(
        [Passage("EMRE", f"§{i}", "v1", "texto", score=i / 10) for i in range(5)]
    )
    assert isinstance(retriever, Retriever)
    assert [p.section for p in retriever.search("gas", k=3)] == ["§4", "§3", "§2"]


def test_transport_roundtrip_and_mtu() -> None:
    async def scenario() -> None:
        transport = LoopbackTransport(mtu=21)
        assert isinstance(transport, Transport)
        await transport.send("B", b"x" * 21)
        assert await transport.receive(timeout=0.1) == ("B", b"x" * 21)
        assert await transport.receive(timeout=0.01) is None
        with pytest.raises(ValueError):
            await transport.send("B", b"x" * 22)

    asyncio.run(scenario())
