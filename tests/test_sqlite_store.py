"""Pruebas propias de SQLiteEventStore (N4). Las de contrato están en test_contracts.py."""

import itertools
import random
import sqlite3
import threading
from pathlib import Path

import pytest

from guadua.agents.correlacion import correlacionar_reporte
from guadua.protocol.header import Incident
from guadua.store.events import Event, EventCategory, EventStore
from guadua.store.sqlite import SCHEMA_VERSION, SQLiteEventStore
from tests.fakes import InMemoryEventStore
from tests.test_correlacion import encode_geohash, make_reporte_normalizado_event


def make_event(origin: str = "A", seq: int = 1, **kw) -> Event:
    return Event(
        origin_node=origin,
        seq=seq,
        category=kw.pop("category", EventCategory.HECHO),
        type=kw.pop("type", "reporte"),
        received_at=kw.pop("received_at", 1_794_573_600.0),
        **kw,
    )


def varied_events() -> list[Event]:
    """Eventos de tres orígenes, con tildes, refs, huecos y payloads anidados."""
    events = []
    for origin, seqs in {"A": [1, 2, 3, 5], "B": [1, 2], "C": [2, 3]}.items():
        for seq in seqs:
            events.append(
                make_event(
                    origin,
                    seq,
                    payload={"text": f"señora atrapada en Milán #{seq}", "n": seq, "x": [1.5]},
                    refs=((origin, seq - 1),) if seq > 1 else (),
                    received_at=1_794_573_600.0 + seq / 3,
                )
            )
    return events


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "nodo-a.sqlite"


# --- Persistencia y configuración ---------------------------------------------------------


def test_survives_restart(db_path: Path) -> None:
    """El nodo se apaga y vuelve: reconstruye su estado desde el archivo."""
    with SQLiteEventStore(db_path) as store:
        for event in varied_events():
            store.append(event)
        before = (store.version_vector(), store.digest(), len(store), store.last_seq("A"))

    with SQLiteEventStore(db_path) as reopened:
        after = (
            reopened.version_vector(),
            reopened.digest(),
            len(reopened),
            reopened.last_seq("A"),
        )
        assert after == before
        assert reopened.get(("A", 5)) == varied_events()[3]
        assert reopened.append(varied_events()[0]) is False  # lo que ya tenía sigue ahí


def test_file_uses_wal_and_full_sync(db_path: Path) -> None:
    with SQLiteEventStore(db_path):
        conn = sqlite3.connect(db_path)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        conn.close()


def test_last_seq_and_len() -> None:
    store = SQLiteEventStore()
    assert store.last_seq("A") == 0 and len(store) == 0
    for event in varied_events():
        store.append(event)
    assert store.last_seq("A") == 5  # el mayor, aunque haya hueco en el 4
    assert store.last_seq("Z") == 0
    assert len(store) == len(varied_events())


def test_rejects_non_event() -> None:
    with pytest.raises(TypeError):
        SQLiteEventStore().append({"origin_node": "A", "seq": 1})  # type: ignore[arg-type]


# --- Inmutabilidad (regla 2) ---------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE events SET type = 'otro' WHERE origin_node = 'A'",
        "DELETE FROM events WHERE origin_node = 'A'",
        "INSERT OR REPLACE INTO events VALUES ('A', 1, 'hecho', 'x', 0, '{}')",
    ],
    ids=["update", "delete", "replace"],
)
def test_database_rejects_edits_and_deletes(db_path: Path, sql: str) -> None:
    with SQLiteEventStore(db_path) as store:
        store.append(make_event())
        conn = sqlite3.connect(db_path)
        with pytest.raises(sqlite3.DatabaseError, match="inmutables"):
            conn.execute(sql)
        conn.close()
        assert store.get(("A", 1)) == make_event()


def test_same_id_other_content_keeps_first(caplog: pytest.LogCaptureFixture) -> None:
    store = SQLiteEventStore()
    first = make_event(payload={"v": 1})
    assert store.append(first) is True
    assert store.append(make_event(payload={"v": 2})) is False
    assert store.get(first.id) == first
    assert "otro contenido" in caplog.text


# --- Igual que el almacén en memoria -------------------------------------------------------


@pytest.mark.parametrize("seed", range(5))
def test_matches_in_memory_store(seed: int) -> None:
    """Mismos eventos en distinto orden y con repetidos: mismo digest, since y vector."""
    events = varied_events()
    shuffled = events + events[:4]
    random.Random(seed).shuffle(shuffled)
    memory, sqlite = InMemoryEventStore(), SQLiteEventStore()
    for event in shuffled:
        assert memory.append(event) == sqlite.append(event)

    assert sqlite.digest() == memory.digest()
    assert sqlite.version_vector() == memory.version_vector() == {"A": 3, "B": 2}
    for vector in [{}, {"A": 2}, {"A": 5, "B": 2, "C": 3}, {"Z": 9}, {"A": 1, "C": 2}]:
        assert sqlite.since(vector) == memory.since(vector)
    for event in events:
        assert sqlite.get(event.id) == memory.get(event.id) == event
    assert sqlite.get(("A", 4)) is None


def test_agent_flow_gives_same_digest_on_both_stores() -> None:
    """El agente de correlación (N9) produce lo mismo sobre SQLite que en memoria."""
    geohash = encode_geohash(5.0687, -75.5174)

    def run(store: EventStore) -> str:
        counter = itertools.count(100)
        for i in range(4):
            event = make_reporte_normalizado_event(
                f"a1b2c3d4-{i}", Incident.COLAPSO, geohash=geohash, seq=i + 1,
                received_at=1_794_573_600.0 + i * 60,
            )  # fmt: skip
            store.append(event)
            correlacionar_reporte(event, store, "A", lambda: next(counter))
        return store.digest()

    memory, sqlite = InMemoryEventStore(), SQLiteEventStore()
    assert run(sqlite) == run(memory)
    assert any(e.type == "incidente_agrupado" for e in sqlite.since({}))


# --- Concurrencia --------------------------------------------------------------------------


def test_reader_is_not_blocked_by_writer(db_path: Path) -> None:
    """WAL: otra conexión lee mientras esta tiene una escritura abierta."""
    with SQLiteEventStore(db_path) as writer, SQLiteEventStore(db_path) as reader:
        writer.append(make_event("A", 1))
        writer._conn.execute("BEGIN IMMEDIATE")  # escritura en curso, sin confirmar
        writer._conn.execute(
            "INSERT INTO events VALUES ('A', 2, 'hecho', 'reporte', 0, ?)",
            (make_event("A", 2).model_dump_json(),),
        )
        assert [e.id for e in reader.since({})] == [("A", 1)]  # lee sin esperar
        writer._conn.execute("COMMIT")
        assert [e.id for e in reader.since({})] == [("A", 1), ("A", 2)]


def test_threads_writing_at_once_lose_nothing(db_path: Path) -> None:
    with SQLiteEventStore(db_path) as store:
        accepted: list[bool] = []

        def write(origin: str) -> None:
            for seq in range(1, 101):
                accepted.append(store.append(make_event(origin, seq)))
                accepted.append(store.append(make_event(origin, seq)))  # repetido

        threads = [threading.Thread(target=write, args=(o,)) for o in "ABCD"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert accepted.count(True) == 400
        assert len(store) == 400
        assert store.version_vector() == {"A": 100, "B": 100, "C": 100, "D": 100}
