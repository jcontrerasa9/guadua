"""Caída y vuelta de un nodo sobre la red simulada, medidas con un reloj simulado (N12).

Lo usan las pruebas de resiliencia y lo reutiliza el banco de fallos (N15). Sin red real ni
esperas: cada ronda adelanta el reloj `interval` segundos, hace una ronda de gossip, entrega
lo que llegó y una ronda de resiliencia en cada nodo vivo.

    clock = SimClock()
    nodes = {n: Node(n, store, transport=network.endpoint(n), peers=..., monotonic=clock)}
    result = await measure_failover(nodes, network, clock, victim="A", restart=volver_a)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass

from guadua.channel.simulated import SimulatedNetwork
from guadua.node.service import Node
from guadua.services.resilience import pending_work


@dataclass(slots=True)
class SimClock:
    """Reloj monotónico simulado para los latidos."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def run_round(nodes: Iterable[Node], clock: SimClock, interval: float = 2.0) -> None:
    nodes = list(nodes)
    clock.advance(interval)
    for node in nodes:
        await node.gossip.tick()
    while sum([await node.gossip.drain() for node in nodes]):
        pass
    for node in nodes:
        node.resilience.check()


def converged(nodes: Iterable[Node]) -> bool:
    return len({node.store.digest() for node in nodes}) == 1


def pending_of(node: Node, owner: str) -> int:
    """Pasos que faltan en el registro de `node` y que son de `owner`."""
    work = pending_work(node.store.since({}), with_triage=node.triaje is not None)
    return sum(node.ownership.owner_of(w.anchor) == owner for w in work)


@dataclass(slots=True)
class FailoverResult:
    victim: str
    taker: str | None  # quién publicó la reasignación (None si no hacía falta)
    pending_before: int  # pasos del caído que faltaban al caer
    pending_after: int  # los que siguen sin hacer tras la toma (0 = se tomó todo)
    detection_s: float | None  # de la caída a la reasignación
    takeover_s: float | None  # de la caída a no quedar pendientes del caído
    recovery_s: float | None  # del regreso a ponerse al día con un vecino (lo mide el nodo)
    converge_s: float | None  # del regreso a que los hashes de todos coincidan
    rounds: int

    def as_dict(self) -> dict:
        return asdict(self)


async def measure_failover(
    nodes: dict[str, Node],
    network: SimulatedNetwork,
    clock: SimClock,
    *,
    victim: str,
    restart: Callable[[], Node] | None = None,
    interval: float = 2.0,
    max_rounds: int = 200,
) -> FailoverResult:
    """Apaga `victim` (lo desconecta y deja de correr), espera a que otro tome su trabajo y,
    si hay `restart`, lo vuelve a levantar con su misma SQLite y espera a converger.
    `nodes[victim]` queda apuntando al nodo nuevo."""
    peers = nodes[victim].gossip.peers
    alive = [n for name, n in nodes.items() if name != victim]
    before = {e.id for n in alive for e in n.store.since({}) if e.type == "reasignacion"}
    pending_before = pending_of(nodes[victim], victim)
    for peer in peers:
        network.disconnect(victim, peer)
    down_at = clock.now
    rounds, taker, detection_s, takeover_s = 0, None, None, None
    timeout = max(n.resilience.timeout for n in alive)
    while rounds < max_rounds:
        await run_round(alive, clock, interval)
        rounds += 1
        if taker is None:
            for n in alive:
                new = [
                    e
                    for e in n.store.since({})
                    if e.type == "reasignacion"
                    and e.payload["from_node"] == victim
                    and e.id not in before
                ]
                if new:
                    taker = min(new, key=lambda e: e.origin_node).origin_node
                    detection_s = clock.now - down_at
                    break
        left = max((pending_of(n, victim) for n in alive), default=0)
        if left == 0 and converged(alive) and (taker or clock.now - down_at > 2 * timeout):
            takeover_s = clock.now - down_at if taker else None
            break
    pending_after = max((pending_of(n, victim) for n in alive), default=0)

    recovery_s = converge_s = None
    if restart is not None:
        nodes[victim] = back = restart()
        for peer in peers:
            network.connect(victim, peer)
        back_at = clock.now
        while rounds < max_rounds:
            await run_round(nodes.values(), clock, interval)
            rounds += 1
            if converged(nodes.values()) and back.resilience.ready():
                converge_s = clock.now - back_at
                break
        recovery_s = back.resilience.stats.recovery_s
    return FailoverResult(
        victim, taker, pending_before, pending_after, detection_s, takeover_s, recovery_s,
        converge_s, rounds,
    )  # fmt: skip
