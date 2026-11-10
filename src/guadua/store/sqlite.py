"""Registro de eventos en SQLite (modo WAL). Implementa `EventStore` (store/events.py).

Un archivo por nodo, sin servidor. Si el nodo se cae, al volver reconstruye su estado desde
el archivo y el gossip le trae solo lo que falta (docs/diseno.md §9).

- La llave es el ID del evento, `(origin_node, seq)`: igual en todos los nodos.
- Se guarda el JSON canónico del evento (`model_dump_json()`); `digest()` lo recorre ordenado
  por ID y da el mismo hash que `InMemoryEventStore` con los mismos eventos.
- Los eventos son inmutables (regla 2): unos triggers rechazan UPDATE, DELETE y sobrescribir
  un ID existente (INSERT OR REPLACE), venga de donde venga.
- `synchronous=FULL`: en un desastre se va la luz; preferimos no perder la última escritura.

    with SQLiteEventStore("data/nodo-a.sqlite") as store:
        store.append(event)
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from pathlib import Path
from types import TracebackType

from guadua.store.events import Event, EventId, VersionVector

logger = logging.getLogger("guadua.store")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    origin_node TEXT    NOT NULL,
    seq         INTEGER NOT NULL CHECK (seq >= 1),
    category    TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    received_at REAL    NOT NULL,
    json        TEXT    NOT NULL,
    PRIMARY KEY (origin_node, seq)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS events_by_type ON events (type);

-- También cubre INSERT OR REPLACE, que borra la fila vieja sin disparar el trigger de DELETE.
CREATE TRIGGER IF NOT EXISTS events_no_overwrite BEFORE INSERT ON events
WHEN EXISTS (SELECT 1 FROM events WHERE origin_node = NEW.origin_node AND seq = NEW.seq)
BEGIN
    SELECT RAISE(ABORT, 'los eventos son inmutables: ya existe');
END;

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'los eventos son inmutables: no se editan');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'los eventos son inmutables: no se borran');
END;
"""


class SQLiteEventStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # --- EventStore ------------------------------------------------------------------------

    def append(self, event: Event) -> bool:
        """Guarda el evento. Devuelve False si ya existía (mismo id); no hace daño."""
        if not isinstance(event, Event):
            raise TypeError(f"se esperaba Event, llegó {type(event).__name__}")
        data = event.model_dump_json()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO events (origin_node, seq, category, type, received_at, json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (event.origin_node, event.seq, event.category, event.type,
                     event.received_at, data),
                )  # fmt: skip
                return True
            except sqlite3.IntegrityError:
                pass  # ya existía: el trigger events_no_overwrite lo rechazó
            row = self._conn.execute(
                "SELECT json FROM events WHERE origin_node = ? AND seq = ?", event.id
            ).fetchone()
        if row and row[0] != data:
            logger.warning(
                "evento %s:%s repetido con otro contenido; se conserva el primero", *event.id
            )
        return False

    def get(self, event_id: EventId) -> Event | None:
        origin, seq = event_id
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM events WHERE origin_node = ? AND seq = ?", (origin, seq)
            ).fetchone()
        return Event.model_validate_json(row[0]) if row else None

    def since(self, vector: VersionVector) -> list[Event]:
        """Eventos que el dueño de `vector` todavía no tiene, ordenados por id."""
        if vector:
            values = ", ".join("(?, ?)" for _ in vector)
            sql = (
                f"WITH known(origin_node, seq) AS (VALUES {values}) "
                "SELECT e.json FROM events e "
                "LEFT JOIN known k ON k.origin_node = e.origin_node "
                "WHERE e.seq > COALESCE(k.seq, 0) "
                "ORDER BY e.origin_node, e.seq"
            )
            params = [item for pair in vector.items() for item in pair]
        else:
            sql, params = "SELECT json FROM events ORDER BY origin_node, seq", []
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Event.model_validate_json(row[0]) for row in rows]

    def version_vector(self) -> VersionVector:
        """Por cada origen, el último consecutivo sin huecos desde 1."""
        sql = (
            "SELECT origin_node, MAX(seq) FROM ("
            "  SELECT origin_node, seq,"
            "         ROW_NUMBER() OVER (PARTITION BY origin_node ORDER BY seq) AS position"
            "  FROM events"
            ") WHERE seq = position GROUP BY origin_node ORDER BY origin_node"
        )
        with self._lock:
            return dict(self._conn.execute(sql).fetchall())

    def digest(self) -> str:
        """SHA-256 del conjunto ordenado de eventos (verificación de convergencia)."""
        sha = hashlib.sha256()
        with self._lock:
            for (data,) in self._conn.execute("SELECT json FROM events ORDER BY origin_node, seq"):
                sha.update(data.encode())
        return sha.hexdigest()

    # --- Ayudas del nodo (no son parte de la interfaz) -------------------------------------

    def last_seq(self, origin_node: str) -> int:
        """Mayor consecutivo guardado de ese origen; 0 si no hay. El nodo retoma desde aquí."""
        with self._lock:
            (value,) = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE origin_node = ?", (origin_node,)
            ).fetchone()
        return value

    def __len__(self) -> int:
        with self._lock:
            (count,) = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return count

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SQLiteEventStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
