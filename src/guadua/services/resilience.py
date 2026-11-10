"""Resiliencia (Agente 5, docs/diseno.md §9 y §10). Servicio sin LLM: ningún LLM decide
sobre la infraestructura.

**Latidos.** Son los vectores que el gossip ya manda cada ronda: `GossipNode.last_seen`
guarda cuándo llegó algo de cada vecino. Un vecino que calla más de `timeout` segundos
está caído.

**Dueño.** El trabajo que dispara un evento (normalizar un reporte, estimar su urgencia,
agruparlo, armar la ficha…) es del nodo que publicó ese evento, salvo que una
`reasignacion` lo cubra: quien la publica toma el trabajo de `from_node` hasta
`up_to_seq`. Si varias cubren el mismo evento, gana la del nodo de identificador más bajo.
La regla sale del registro, así que todos los nodos llegan a la misma conclusión, también
el que vuelve. Se encadena: si B tomó lo de A y luego cae B, quien tome lo de B (que cubre
la reasignación de B) se queda también con lo de A.

**Pendiente.** Un evento cuyo siguiente paso no está en el registro, lo haya hecho quien
lo haya hecho (ver `pending_work`). Revisar el registro antes de actuar evita repetir lo
que otro ya hizo.

**Recuperación.** Un nodo que arranca (o vuelve) espera a ponerse al día con un vecino
antes de retomar sus pendientes, para ver primero las reasignaciones. Si no tiene con
quién sincronizar, retoma igual. El tiempo de recuperación se mide en `ResilienceStats`.

Un error aquí nunca tumba el nodo, el ACK ni el gossip.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Literal

from guadua.agents.recepcion import needs_question
from guadua.store.event_types import Reasignacion, Reporte, ReporteNormalizado
from guadua.store.events import Event, EventCategory, EventId

if TYPE_CHECKING:
    from guadua.node.service import Node

logger = logging.getLogger("guadua.resilience")

DEFAULT_PEER_TIMEOUT = 10.0  # s sin latidos para dar a un vecino por caído (5 rondas de 2 s)

type WorkKind = Literal[
    "normalizar", "urgencia", "agrupar", "preguntar", "triaje", "ficha", "replanear"
]


# --- Dueño ------------------------------------------------------------------------------------


class Ownership:
    """Quién es dueño del trabajo que dispara cada evento, según las reasignaciones vistas."""

    def __init__(self, events: Iterable[Event] = ()) -> None:
        # nodo caído → [(up_to_seq, nodo que lo toma, seq de la reasignación)]
        self._takeovers: dict[str, list[tuple[int, str, int]]] = defaultdict(list)
        for event in events:
            self.add(event)

    def add(self, event: Event) -> None:
        if event.type == "reasignacion":
            p = event.payload
            self._takeovers[p["from_node"]].append((p["up_to_seq"], event.origin_node, event.seq))

    def covered_up_to(self, node: str, by: str) -> int:
        """Hasta qué consecutivo de `node` ya reasignó `by`."""
        return max((up for up, to, _ in self._takeovers.get(node, ()) if to == by), default=0)

    def owner_of(self, event_id: EventId) -> str:
        node, seq = event_id
        seen: set[EventId] = set()
        while (node, seq) not in seen:
            seen.add((node, seq))
            covering = [(to, s) for up, to, s in self._takeovers.get(node, ()) if seq <= up]
            if not covering:
                return node
            node, seq = min(covering)  # el de id más bajo; y su reasignación puede estar cubierta
        return node


# --- Pendientes ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Work:
    """Un paso que falta. `anchor` es el evento que decide de quién es el paso."""

    kind: WorkKind
    event: Event
    anchor: EventId

    @property
    def key(self) -> tuple[str, EventId]:
        return self.kind, self.event.id


def _order(event: Event) -> tuple[float, str, int]:
    return event.received_at, event.origin_node, event.seq


def pending_work(events: Iterable[Event], *, with_triage: bool = True) -> list[Work]:
    """Pasos que faltan en el registro, en el orden de la cadena:

    - `reporte` sin `reporte_normalizado` de su report_id → normalizar
    - `reporte_normalizado` sin `urgencia_estimada` → urgencia; sin `incidente_agrupado`
      que lo incluya → agrupar; sin `pregunta_seguimiento` cuando las reglas piden una
      (D5a) → preguntar
    - `urgencia_estimada` ambigua de N7 que ninguna otra reemplaza → triaje (solo si hay
      agente: sin LLM el caso ya quedó "ambiguo, revisar", que es seguro)
    - último `incidente_agrupado` de un incidente sin `ficha` → ficha
    - `decision` "rechazar" sobre la última ficha del incidente → replanear
    """
    events = list(events)
    normalized: set[str] = set()
    estimated: set[str] = set()
    grouped: set[str] = set()
    asked: set[str] = set()
    replaced: set[EventId] = set()
    incidents: dict[str, Event] = {}
    fichas: dict[str, list[Event]] = defaultdict(list)
    by_id = {e.id: e for e in events}
    for e in events:
        match e.type:
            case "reporte_normalizado":
                normalized.add(e.payload["report_id"])
            case "urgencia_estimada":
                estimated.add(e.payload["report_id"])
                replaced.update(e.refs)
            case "pregunta_seguimiento":
                asked.add(e.payload["report_id"])
            case "incidente_agrupado":
                grouped.update(e.payload["report_ids"])
                iid = e.payload["incident_id"]
                if iid not in incidents or _order(e) > _order(incidents[iid]):
                    incidents[iid] = e
            case "ficha":
                fichas[e.payload["incident_id"]].append(e)

    work: list[Work] = []
    for e in events:
        rid = e.payload.get("report_id")
        if e.type == "reporte" and rid not in normalized:
            work.append(Work("normalizar", e, e.id))
        elif e.type == "reporte_normalizado":
            if rid not in estimated:
                work.append(Work("urgencia", e, e.id))
            if rid not in grouped:
                work.append(Work("agrupar", e, e.id))
            if rid not in asked and _needs_question(e, by_id):
                work.append(Work("preguntar", e, e.id))
        elif (
            e.type == "urgencia_estimada"
            and with_triage
            and e.id not in replaced
            and e.payload["ambiguous"]
            and e.payload["source"] != "agente"  # = agents.triaje.needs_agent
        ):
            work.append(Work("triaje", e, e.id))
    for iid, incident in incidents.items():
        if not fichas[iid]:
            work.append(Work("ficha", incident, incident.id))
    for e in events:
        if e.type != "decision" or not e.refs or e.payload["action"] != "rechazar":
            continue
        ficha = by_id.get(e.refs[0])
        if ficha is None or ficha.type != "ficha":
            continue
        if max(fichas[ficha.payload["incident_id"]], key=_order).id == ficha.id:
            work.append(Work("replanear", e, ficha.id))
    return work


def _needs_question(normalized: Event, by_id: dict[EventId, Event]) -> bool:
    reporte = by_id.get(normalized.refs[0]) if normalized.refs else None
    if reporte is None or reporte.type != "reporte":
        return False
    return needs_question(
        Reporte.model_validate(reporte.payload),
        ReporteNormalizado.model_validate(normalized.payload),
    )


# --- Servicio -----------------------------------------------------------------------------


@dataclass(slots=True)
class Takeover:
    from_node: str
    up_to_seq: int
    silent_s: float  # cuánto calló el vecino antes de declararlo caído (detección)
    event_id: str
    steps: int = 0  # pasos pendientes que se hicieron en la toma
    took_s: float = 0.0  # cuánto tardó la toma, desde publicar la reasignación


@dataclass(slots=True)
class ResilienceStats:
    """Lo que mide el Agente 5. N15 lo lee para su tabla de métricas (§15)."""

    restarted: bool  # arrancó con eventos en su SQLite (vuelve de una caída)
    recovery_s: float | None = None  # del arranque a ponerse al día con un vecino
    takeovers: list[Takeover] = field(default_factory=list)
    resumed: int = 0  # pasos propios retomados (p. ej., el triaje que se cortó al caer)
    failed: int = 0  # pasos que fallaron (se intentan una vez por proceso)

    def as_dict(self) -> dict:
        return asdict(self)


class Resilience:
    """Detecta vecinos caídos, toma su trabajo pendiente y retoma el propio."""

    def __init__(
        self,
        node: Node,
        *,
        timeout: float = DEFAULT_PEER_TIMEOUT,
        monotonic: Callable[[], float] = time.monotonic,
        restarted: bool = False,
    ) -> None:
        self.node = node
        self.timeout = timeout
        self.monotonic = monotonic
        self.started_at = monotonic()
        self.stats = ResilienceStats(restarted=restarted)
        self._ready = False
        self._tried: set[tuple[str, EventId]] = set()

    # --- Latidos ---------------------------------------------------------------------------

    def silent_for(self, peer: str, now: float | None = None) -> float:
        """Segundos sin oír a `peer` (desde el arranque si nunca se le oyó)."""
        now = self.monotonic() if now is None else now
        gossip = self.node.gossip
        last = gossip.last_seen.get(peer, self.started_at) if gossip else self.started_at
        return now - last

    def dead_peers(self, now: float | None = None) -> tuple[str, ...]:
        gossip = self.node.gossip
        if gossip is None:
            return ()
        return tuple(p for p in gossip.peers if self.silent_for(p, now) > self.timeout)

    def ready(self, now: float | None = None) -> bool:
        """¿Ya puede actuar? Al ponerse al día con un vecino, o si no tiene con quién."""
        if self._ready:
            return True
        gossip = self.node.gossip
        if gossip is None or not gossip.peers:
            self._ready = True
        elif gossip.caught_up:
            self._ready = True
            self.stats.recovery_s = min(gossip.caught_up.values()) - self.started_at
            logger.warning(
                "%s: %s en %.1f s",
                self.node.node_id,
                "recuperado y al día" if self.stats.restarted else "al día con los vecinos",
                self.stats.recovery_s,
            )
        elif len(self.dead_peers(now)) == len(gossip.peers):
            self._ready = True
            logger.warning("%s: ningún vecino responde; sigue solo", self.node.node_id)
        return self._ready

    # --- Ronda -----------------------------------------------------------------------------

    def check(self) -> list[Work]:
        """Una ronda: detecta caídos, toma su trabajo y hace los pasos propios pendientes.
        Devuelve los pasos que intentó. Nunca lanza."""
        try:
            return self._check()
        except Exception:
            logger.exception("%s: falló la ronda de resiliencia", self.node.node_id)
            return []

    async def run(self, interval: float = 2.0, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            self.check()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)

    def _check(self) -> list[Work]:
        now = self.monotonic()
        if not self.ready(now):
            return []
        node = self.node
        with_triage = node.triaje is not None
        new: list[Takeover] = []
        dead = self.dead_peers(now)
        if dead:
            pending = pending_work(node.store.since({}), with_triage=with_triage)
            for peer in dead:
                theirs = any(node.ownership.owner_of(w.anchor) == peer for w in pending)
                if theirs and (takeover := self._take(peer, now)) is not None:
                    new.append(takeover)

        started = self.monotonic()
        todo = [
            w
            for w in pending_work(node.store.since({}), with_triage=with_triage)
            if w.key not in self._tried
            and node.owns(w.anchor)
            and not (w.kind == "triaje" and w.event.id in node.triage_running)
            and not (w.kind == "preguntar" and w.event.id in node.asking)
        ]
        done = 0
        for work in todo:
            self._tried.add(work.key)
            try:
                node.do_work(work)
                done += 1
            except Exception:
                self.stats.failed += 1
                logger.exception("%s: falló el paso %s de %s:%s", node.node_id, work.kind,
                                 *work.event.id)  # fmt: skip
        took = self.monotonic() - started
        if new:
            new[-1].steps, new[-1].took_s = done, took
            for t in new:
                logger.warning(
                    "%s: tomó el trabajo de %s hasta %s:%d (%d pasos en %.2f s; %s callado %.1f s)",
                    node.node_id, t.from_node, t.from_node, t.up_to_seq, t.steps, t.took_s,
                    t.from_node, t.silent_s,
                )  # fmt: skip
        elif done:
            self.stats.resumed += done
            logger.warning("%s: retomó %d pasos pendientes", node.node_id, done)
        return todo

    def _take(self, peer: str, now: float) -> Takeover | None:
        node = self.node
        # Hasta el último evento suyo que se conoce, aunque haya huecos: lo que falte y
        # llegue después también queda cubierto.
        up_to = max((e.seq for e in node.store.since({}) if e.origin_node == peer), default=0)
        if up_to < 1 or up_to <= node.ownership.covered_up_to(peer, node.node_id):
            return None
        silent = round(self.silent_for(peer, now), 3)
        payload = Reasignacion(from_node=peer, up_to_seq=up_to, silent_s=silent)
        event = Event(
            origin_node=node.node_id,
            seq=node.next_seq(),
            category=EventCategory.HECHO,
            type="reasignacion",
            payload=payload.model_dump(mode="json"),
            refs=((peer, up_to),),
            received_at=node.clock(),
        )
        node.record(event)
        takeover = Takeover(peer, up_to, silent, f"{event.origin_node}:{event.seq}")
        self.stats.takeovers.append(takeover)
        return takeover
