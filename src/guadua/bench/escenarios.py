"""Escenarios del banco de fallos (N15). Cada uno arma su red simulada con la semilla dada.

Todo corre con un reloj simulado (`SimClock`) que también mueve la red (`time_scale=1`): los
retrasos del perfil cuentan y pueden desordenar tramas, pero no hay esperas reales. Una ronda
de gossip y resiliencia son 2 s, como en el nodo (`SARD_GOSSIP_INTERVAL`).

- `run_send`: una ráfaga de reportes sale por la cola de comunicación (N11) del emisor E
  hacia el nodo A, que confirma con ACK. Mide entrega y latencia por urgencia.
- `run_replication`: lo que entra por A llega a C por gossip (N5) en A ↔ B ↔ C.
- `run_partition`: B y C se separan un rato, cada lado recibe reportes y luego se juntan.
- `run_failover`: A cae con trabajo sin hacer, B lo toma y A vuelve (N12, `measure_failover`).
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from guadua.channel.profiles import ChannelProfile
from guadua.channel.simulated import SimulatedNetwork
from guadua.classifier.evaluate import as_reporte
from guadua.classifier.synthetic import Row
from guadua.classifier.triage import UrgencyClassifier, triage
from guadua.classifier.urgency import Urgency
from guadua.node.intake import IntakeError, parse_message, reporte_event
from guadua.node.service import Node
from guadua.protocol.fragments import Ack, encode_ack
from guadua.protocol.header import Flag, Header, MessageType, People, encode
from guadua.protocol.text import TextError, encode_text
from guadua.services.failover import (
    FailoverResult,
    SimClock,
    converged,
    measure_failover,
    run_round,
)
from guadua.services.queue import CommunicationQueue, ItemStatus
from guadua.store.sqlite import SQLiteEventStore

NOW = 1_794_573_600.0  # hora del nodo al empezar cada escenario (segundos Unix)
INTERVAL = 2.0  # segundos por ronda de gossip y resiliencia
PEERS = {"A": ("B",), "B": ("A", "C"), "C": ("B",)}
LINKS = (("A", "B"), ("B", "C"))
LEVELS = (Urgency.CRITICA, Urgency.ALTA, Urgency.MEDIA, Urgency.BAJA)


# --- Canales y reportes -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Channel:
    """Un perfil con la pérdida que se configura en una fila de la tabla."""

    profile: ChannelProfile
    label: str  # "0 %", "30 %", "60 %" o "cortes" (el perfil tal cual)

    @property
    def name(self) -> str:
        return self.profile.name


def channels(
    profiles: Sequence[ChannelProfile], losses: Sequence[float], *, cuts: bool = True
) -> list[Channel]:
    """Por perfil: una fila por pérdida (pérdida suelta, sin cortes, para que lo configurado
    sea lo que se mide) y, con `cuts`, una fila con el perfil tal cual, con sus cortes."""
    out = []
    for profile in profiles:
        for loss in losses:
            tuned = profile.with_changes(loss=loss, cut_rate=0.0)
            out.append(Channel(tuned, f"{round(loss * 100)} %"))
        if cuts:
            out.append(Channel(profile, "cortes"))
    return out


@dataclass(frozen=True, slots=True)
class Sample:
    payload: bytes  # cabecera de 21 B + texto comprimido, como lo envía la PWA
    urgency: Urgency  # la que publica N7 (reglas + clasificador), con la que se encola
    device_id: int
    seq: int


def message_of(row: Row, device_id: int, seq: int) -> bytes:
    """Una fila de los datos sintéticos como mensaje binario de reporte (sin GPS)."""
    header = Header(
        MessageType.REPORTE, device_id, seq, 3600, row.incident, People.NO_SE, Flag(0), barrio=0
    )
    return encode(header) + encode_text(row.text)


def fits(row: Row) -> bool:
    try:
        encode_text(row.text)
    except TextError:
        return False
    return True


def sample_reports(
    rows: Sequence[Row],
    classifier: UrgencyClassifier | None,
    n: int,
    seed: int,
    *,
    device_id: int = 0xBE000000,
) -> list[Sample]:
    """`n` reportes del conjunto de prueba, repartidos por igual entre las cuatro urgencias
    reales y en orden aleatorio (con la semilla). Se encolan con la urgencia que publica N7."""
    rng = random.Random(f"{seed}:reportes:{device_id}")
    test = [r for r in rows if r.split == "test" and fits(r)]
    chosen: list[Row] = []
    for i, level in enumerate(LEVELS):
        pool = [r for r in test if r.label == level]
        chosen += rng.sample(pool, n // len(LEVELS) + (i < n % len(LEVELS)))
    rng.shuffle(chosen)
    out = []
    for seq, row in enumerate(chosen, start=1):
        level = triage(as_reporte(row, seq), classifier).level
        out.append(Sample(message_of(row, device_id, seq), level, device_id, seq))
    return out


def percentile(values: Sequence[float], p: float) -> float | None:
    """Percentil por rango más cercano (sin interpolar): siempre es un valor medido."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


# --- Medidas comunes ----------------------------------------------------------------------


@dataclass(slots=True)
class Traffic:
    frames: int = 0  # tramas enviadas, contando las que se pierden
    lost: int = 0

    @property
    def loss(self) -> float:
        return self.lost / self.frames if self.frames else 0.0


def traffic(network: SimulatedNetwork, links: Sequence[tuple[str, str]]) -> Traffic:
    out = Traffic()
    for a, b in links:
        for src, dst in ((a, b), (b, a)):
            stats = network.stats(src, dst)
            out.frames += stats.sent
            out.lost += stats.lost
    return out


def _cluster(
    channel: Channel,
    seed: int,
    classifier: UrgencyClassifier | None,
    store_dir: Path | None = None,
) -> tuple[SimClock, SimulatedNetwork, dict[str, Node], object]:
    clock = SimClock()
    network = SimulatedNetwork(seed=seed, time_scale=1.0, clock=clock)
    for a, b in LINKS:
        network.link(a, b, channel.profile)

    def start(name: str) -> Node:
        store = SQLiteEventStore(store_dir / f"{name}.db") if store_dir else SQLiteEventStore()
        return Node(
            name,
            store,
            transport=network.endpoint(name),
            peers=PEERS[name],
            clock=lambda: NOW + clock.now,
            classifier=classifier,
            monotonic=clock,
        )

    return clock, network, {n: start(n) for n in "ABC"}, start


def _count(node: Node, event_type: str) -> int:
    return sum(e.type == event_type for e in node.store.since({}))


def parallel_steps(node: Node) -> int:
    """Pasos de un mismo reporte hechos por más de un nodo (normalizado o urgencia)."""
    origins: dict[tuple[str, str], set[str]] = {}
    for e in node.store.since({}):
        if e.type in ("reporte_normalizado", "urgencia_estimada"):
            origins.setdefault((e.type, e.payload["report_id"]), set()).add(e.origin_node)
    return sum(len(o) > 1 for o in origins.values())


# --- Envío: cola de comunicación (N11) ----------------------------------------------------


@dataclass(slots=True)
class SendResult:
    channel: Channel
    reports: int
    delivered: int  # llegaron al nodo A dentro del horizonte
    confirmed: int  # el emisor recibió su ACK
    frames: int  # tramas del emisor (reportes y reenvíos)
    measured_loss: float  # emisor → A
    horizon_s: float
    latencies: dict[Urgency, list[float]] = field(default_factory=dict)  # hasta llegar a A
    missing: dict[Urgency, int] = field(default_factory=dict)  # no llegaron en el horizonte

    @property
    def delivery_rate(self) -> float:
        return self.delivered / self.reports if self.reports else 0.0


async def run_send(
    channel: Channel,
    samples: Sequence[Sample],
    seed: int,
    classifier: UrgencyClassifier | None,
    *,
    horizon_s: float = 1800.0,
    step_s: float = 1.0,
) -> SendResult:
    """La ráfaga entra completa a la cola en t = 0. Cada `step_s` el emisor transmite lo que
    elige la cola; A guarda el reporte (`Node.receive_message`) y responde con ACK."""
    clock = SimClock()
    network = SimulatedNetwork(seed=seed, time_scale=1.0, clock=clock)
    network.link("E", "A", channel.profile)
    sender, receiver = network.endpoint("E"), network.endpoint("A")
    node = Node("A", SQLiteEventStore(), clock=lambda: NOW + clock.now, classifier=classifier)
    queue = CommunicationQueue(clock=clock)
    items = [queue.enqueue(s.payload, destination="A", urgency=s.urgency) for s in samples]
    urgency_of = {(s.device_id, s.seq): s.urgency for s in samples}
    arrived: dict[tuple[int, int], float] = {}

    while clock.now < horizon_s and any(it.status != ItemStatus.ENTREGADO for it in items):
        await queue.transmit_next(sender)
        clock.advance(step_s)
        while (got := await receiver.receive(timeout=0)) is not None:
            try:
                message = parse_message(got[1])
            except IntakeError:
                continue
            key = (message.header.device_id, message.header.seq)
            arrived.setdefault(key, clock.now)
            node.receive_message(got[1])
            ack = Ack(MessageType.REPORTE, *key, total=1, received=frozenset({0}))
            await receiver.send("E", encode_ack(ack))
        while (got := await sender.receive(timeout=0)) is not None:
            queue.process_incoming_frame(got[1])

    latencies: dict[Urgency, list[float]] = {lvl: [] for lvl in LEVELS}
    missing = dict.fromkeys(LEVELS, 0)
    for key, level in urgency_of.items():
        if key in arrived:
            latencies[level].append(arrived[key])
        else:
            missing[level] += 1
    stats = network.stats("E", "A")
    return SendResult(
        channel=channel,
        reports=len(samples),
        delivered=len(arrived),
        confirmed=sum(it.status == ItemStatus.ENTREGADO for it in items),
        frames=stats.sent,
        measured_loss=stats.measured_loss,
        horizon_s=horizon_s,
        latencies=latencies,
        missing=missing,
    )


# --- Replicación: gossip (N5) -------------------------------------------------------------


@dataclass(slots=True)
class ReplicationResult:
    channel: Channel
    reports: int
    complete_s: float | None  # hasta que C tiene todos los reportes
    converge_s: float | None  # hasta que los tres hashes coinciden
    rounds: int
    frames: int
    measured_loss: float
    duplicates: int  # eventos que un nodo ya tenía
    invalid: int  # eventos descartados por no cumplir el catálogo
    takeovers: int  # reasignaciones publicadas (un vecino vivo dado por caído)


async def run_replication(
    channel: Channel,
    samples: Sequence[Sample],
    seed: int,
    classifier: UrgencyClassifier | None,
    *,
    max_rounds: int = 600,
) -> ReplicationResult:
    clock, network, nodes, _ = _cluster(channel, seed, classifier)
    for s in samples:
        nodes["A"].receive_message(s.payload)
    complete_s = converge_s = None
    rounds = 0
    while rounds < max_rounds:
        await run_round(nodes.values(), clock, INTERVAL)
        rounds += 1
        if complete_s is None and _count(nodes["C"], "reporte") == len(samples):
            complete_s = clock.now
        if complete_s is not None and converged(nodes.values()):
            converge_s = clock.now
            break
    t = traffic(network, LINKS)
    return ReplicationResult(
        channel=channel,
        reports=len(samples),
        complete_s=complete_s,
        converge_s=converge_s,
        rounds=rounds,
        frames=t.frames,
        measured_loss=t.loss,
        duplicates=sum(n.gossip.stats.duplicates for n in nodes.values()),
        invalid=sum(n.gossip.stats.invalid for n in nodes.values()),
        takeovers=max(_count(n, "reasignacion") for n in nodes.values()),
    )


# --- Partición B | C ----------------------------------------------------------------------


@dataclass(slots=True)
class PartitionResult:
    channel: Channel
    reports_per_side: int
    split_s: float  # cuánto duró la partición
    converge_s: float | None  # de la reconexión a que los tres hashes coinciden
    rounds: int  # rondas tras la reconexión
    frames: int  # tramas tras la reconexión
    takeovers: int  # reasignaciones publicadas durante todo el escenario
    parallel: int  # pasos de un mismo reporte hechos por dos nodos


async def run_partition(
    channel: Channel,
    side_a: Sequence[Sample],
    side_c: Sequence[Sample],
    seed: int,
    classifier: UrgencyClassifier | None,
    *,
    split_rounds: int = 30,
    max_rounds: int = 600,
) -> PartitionResult:
    """Se corta B ↔ C; entran reportes por A y por C; tras `split_rounds` rondas se
    reconecta y se mide cuánto tardan en converger."""
    clock, network, nodes, _ = _cluster(channel, seed, classifier)
    network.disconnect("B", "C")
    for s in side_a:
        nodes["A"].receive_message(s.payload)
    for s in side_c:
        nodes["C"].receive_message(s.payload)
    for _ in range(split_rounds):
        await run_round(nodes.values(), clock, INTERVAL)
    before = traffic(network, LINKS).frames
    network.connect("B", "C")
    joined_at = clock.now
    converge_s, rounds = None, 0
    while rounds < max_rounds:
        await run_round(nodes.values(), clock, INTERVAL)
        rounds += 1
        if converged(nodes.values()):
            converge_s = clock.now - joined_at
            break
    return PartitionResult(
        channel=channel,
        reports_per_side=len(side_a),
        split_s=split_rounds * INTERVAL,
        converge_s=converge_s,
        rounds=rounds,
        frames=traffic(network, LINKS).frames - before,
        takeovers=max(_count(n, "reasignacion") for n in nodes.values()),
        parallel=max(parallel_steps(n) for n in nodes.values()),
    )


# --- Caída de A (N12) ---------------------------------------------------------------------


@dataclass(slots=True)
class FailoverRun:
    channel: Channel
    spread_s: float  # cuánto tardó lo de A en llegar a C antes de la caída
    result: FailoverResult


async def run_failover(
    channel: Channel,
    samples: Sequence[Sample],
    seed: int,
    classifier: UrgencyClassifier | None,
    store_dir: Path,
    *,
    max_rounds: int = 600,
) -> FailoverRun:
    """A guarda los reportes y cae antes de procesarlos (como en tests/test_resilience.py):
    primero se difunden sin rondas de resiliencia, luego `measure_failover` apaga A, espera la
    toma de B y lo vuelve a levantar con su misma SQLite."""
    clock, network, nodes, start = _cluster(channel, seed, classifier, store_dir)
    a = nodes["A"]
    for s in samples:
        event = reporte_event(
            parse_message(s.payload), node_id="A", seq=a.next_seq(), received_at=NOW
        )
        a.store.append(event)
    for _ in range(max_rounds):
        if _count(nodes["C"], "reporte") == len(samples):
            break
        clock.advance(INTERVAL)
        for node in nodes.values():
            await node.gossip.tick()
        while sum([await node.gossip.drain() for node in nodes.values()]):
            pass
    spread_s = clock.now
    result = await measure_failover(
        nodes,
        network,
        clock,
        victim="A",
        restart=lambda: start("A"),
        interval=INTERVAL,
        max_rounds=max_rounds,
    )
    return FailoverRun(channel, spread_s, result)
