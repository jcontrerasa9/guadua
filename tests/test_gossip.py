"""Pruebas del gossip con vectores de versión (N5)."""

import asyncio
import json
from pathlib import Path

import msgpack
import pytest

from guadua.channel.profiles import IDEAL, LORA
from guadua.channel.simulated import SimulatedNetwork
from guadua.store.events import Event, EventStore
from guadua.store.gossip import (
    GOSSIP_PREFIX,
    EventMessage,
    GossipError,
    GossipNode,
    VectorMessage,
    decode_message,
    encode_event,
    encode_vector,
)
from guadua.store.sqlite import SQLiteEventStore
from tests.fakes import InMemoryEventStore

EXAMPLES = json.loads(
    (Path(__file__).parent / "fixtures" / "event_examples.json").read_text(encoding="utf-8")
)
EVENTS = [Event.model_validate(e) for e in EXAMPLES["valid"]]
LORA_60 = LORA.with_loss(0.6)


def reporte(origin: str, seq: int, text: str = "colapso en Chipre") -> Event:
    """Un reporte válido del catálogo, creado en `origin`."""
    base = EVENTS[0]
    payload = {**base.payload, "report_id": f"0000000{seq % 10}-{seq}", "text": text}
    return base.model_copy(update={"origin_node": origin, "seq": seq, "payload": payload})


# --- Arnés: tres nodos A ↔ B ↔ C sobre el canal simulado -----------------------------------


class Cluster:
    def __init__(self, profile=IDEAL, seed=0, stores=None, topology=("AB", "BC")) -> None:
        self.network = SimulatedNetwork(seed=seed, time_scale=0)
        for a, b in topology:
            self.network.link(a, b, profile)
        names = sorted({n for link in topology for n in link})
        peers = {n: [m for link in topology for m in link if n in link and m != n] for n in names}
        stores = stores or {n: InMemoryEventStore() for n in names}
        self.nodes = {
            n: GossipNode(n, stores[n], self.network.endpoint(n), peers[n]) for n in names
        }

    def store(self, name: str) -> EventStore:
        return self.nodes[name].store

    def digests(self) -> set[str]:
        return {node.store.digest() for node in self.nodes.values()}

    async def drain(self) -> None:
        while True:
            processed = 0
            for node in self.nodes.values():
                processed += await node.drain()
            if not processed:
                return

    async def round(self) -> None:
        for node in self.nodes.values():
            await node.tick()
        await self.drain()

    async def converge(self, max_rounds: int = 500) -> int:
        """Rondas hasta que los tres digests coinciden."""
        for rounds in range(1, max_rounds + 1):
            await self.round()
            if len(self.digests()) == 1:
                return rounds
        raise AssertionError(f"no convergió en {max_rounds} rondas")


def run(coro):
    return asyncio.run(coro)


# --- Códec ----------------------------------------------------------------------------------


def test_vector_round_trip() -> None:
    store = InMemoryEventStore()
    for event in EVENTS:
        store.append(event)
    frame = encode_vector(store.version_vector(), store.digest())
    assert frame[0] == GOSSIP_PREFIX == 0x15
    message = decode_message(frame)
    assert message == VectorMessage(store.version_vector(), bytes.fromhex(store.digest())[:8])


@pytest.mark.parametrize("event", EVENTS, ids=lambda e: e.type)
def test_event_round_trip_keeps_digest(event: Event) -> None:
    """Lo que llega por gossip se guarda idéntico: mismo JSON, mismo digest."""
    message = decode_message(encode_event(event))
    assert isinstance(message, EventMessage)
    assert message.event == event
    assert message.event.model_dump_json() == event.model_dump_json()


def test_event_with_accents_floats_and_refs_round_trips() -> None:
    event = reporte("C", 3, "señora atrapada en Milán, ¿huele a gas? 🆘").model_copy(
        update={"received_at": 1_794_573_600.123456, "refs": (("A", 1), ("B", 2))}
    )
    assert decode_message(encode_event(event)).event.model_dump_json() == event.model_dump_json()


def _raw(body) -> bytes:
    return bytes([GOSSIP_PREFIX]) + msgpack.packb(body, use_bin_type=True)


@pytest.mark.parametrize(
    "frame",
    [
        b"",
        b"\x10" + bytes(20),  # una cabecera de reporte, no gossip
        b"\x25" + _raw({"k": "v"})[1:],  # versión desconocida
        _raw([1, 2]),
        _raw({"k": "x"}),
        _raw({"k": "v", "vv": {"A": 1}}),  # falta el digest
        _raw({"k": "v", "vv": {"A": 1}, "d": b"1234"}),  # digest corto
        _raw({"k": "v", "vv": {"A": -1}, "d": bytes(8)}),
        _raw({"k": "v", "vv": {"A": True}, "d": bytes(8)}),
        _raw({"k": "v", "vv": [1], "d": bytes(8)}),
        _raw({"k": "v", "vv": {"": 1}, "d": bytes(8)}),
        _raw({"k": "v", "vv": {}, "d": bytes(8), "extra": 1}),  # campo desconocido
        _raw({"k": "e", "ev": {"origin_node": "A"}}),
        bytes([GOSSIP_PREFIX]) + b"\xc1",  # MessagePack inválido
    ],
)
def test_decode_rejects(frame: bytes) -> None:
    with pytest.raises(GossipError):
        decode_message(frame)


def test_reports_need_fragments_on_lora() -> None:
    """Un reporte no cabe en una trama LoRa: el gossip tiene que fragmentarlo."""
    assert len(encode_event(EVENTS[0])) > LORA.mtu


# --- Sincronización -------------------------------------------------------------------------


def test_two_nodes_converge_in_one_round_on_ideal_link() -> None:
    async def scenario() -> None:
        cluster = Cluster(IDEAL, topology=("AB",))
        for event in EVENTS:
            cluster.store("A").append(event)
        cluster.store("B").append(reporte("B", 1))
        assert await cluster.converge() == 1
        assert len(cluster.digests()) == 1

    run(scenario())


@pytest.mark.parametrize("seed", range(5))
def test_a_reaches_c_through_b_with_60_percent_loss(seed: int) -> None:
    """Terminado cuando de N5: lo que entra por A llega a C; los 3 hashes coinciden tras
    60 % de pérdida, sin enlace directo A–C."""

    async def scenario() -> int:
        cluster = Cluster(LORA_60, seed=seed)
        for event in EVENTS:
            cluster.store("A").append(event)
        for seq in range(1, 4):
            cluster.store("C").append(reporte("C", 100 + seq))
        senders_to_c = set()
        cluster.network.log.subscribe(
            lambda e: senders_to_c.add(e.link.split("→")[0]) if e.link.endswith("→C") else None
        )
        rounds = await cluster.converge()

        assert len(cluster.digests()) == 1
        for event in EVENTS:
            assert cluster.store("C").get(event.id) == event  # lo de A llegó a C
        assert cluster.store("A").get(("C", 101)) is not None  # y lo de C llegó a A
        assert senders_to_c == {"B"}  # C solo recibió tramas de B
        with pytest.raises(ValueError):
            cluster.network.profile("A", "C")  # no existe el enlace A–C
        loss = cluster.network.stats("A", "B").measured_loss
        assert 0.5 <= loss <= 0.7
        print(f"semilla {seed}: convergió en {rounds} rondas, pérdida A→B {loss:.0%}")
        return rounds

    run(scenario())


def test_partition_then_reconnect_converges() -> None:
    async def scenario() -> None:
        cluster = Cluster(LORA_60, seed=7)
        cluster.store("A").append(reporte("A", 1))
        await cluster.converge()
        cluster.network.disconnect("B", "C")
        cluster.store("A").append(reporte("A", 2))
        cluster.store("C").append(reporte("C", 1))
        for _ in range(30):
            await cluster.round()
        assert cluster.store("C").get(("A", 2)) is None  # partición: no pasa nada
        assert cluster.store("B").get(("A", 2)) is not None  # pero A y B sí se hablan
        cluster.network.connect("B", "C")
        await cluster.converge()
        assert cluster.store("A").get(("C", 1)) is not None

    run(scenario())


def test_in_sync_round_sends_no_events() -> None:
    async def scenario() -> None:
        cluster = Cluster(IDEAL)
        for event in EVENTS:
            cluster.store("A").append(event)
        await cluster.converge()
        before = {n: node.stats.events_sent for n, node in cluster.nodes.items()}
        await cluster.round()
        after = {n: node.stats.events_sent for n, node in cluster.nodes.items()}
        assert before == after
        assert all(node.stats.in_sync_skips > 0 for node in cluster.nodes.values())

    run(scenario())


def test_duplicates_are_harmless() -> None:
    async def scenario() -> None:
        cluster = Cluster(IDEAL.with_changes(duplicate=1.0))
        for event in EVENTS:
            cluster.store("A").append(event)
        await cluster.converge()
        assert len(cluster.digests()) == 1
        assert sum(node.stats.duplicates for node in cluster.nodes.values()) > 0
        assert len(cluster.store("C").since({})) == len(EVENTS)

    run(scenario())


def test_mixed_sqlite_and_memory_stores_converge(tmp_path: Path) -> None:
    async def scenario() -> None:
        stores = {
            "A": SQLiteEventStore(tmp_path / "a.sqlite"),
            "B": InMemoryEventStore(),
            "C": SQLiteEventStore(tmp_path / "c.sqlite"),
        }
        cluster = Cluster(LORA_60, seed=3, stores=stores)
        for event in EVENTS:
            stores["A"].append(event)
        await cluster.converge()
        assert len(cluster.digests()) == 1

    run(scenario())


def test_on_event_is_called_once_per_new_event() -> None:
    async def scenario() -> None:
        cluster = Cluster(IDEAL.with_changes(duplicate=1.0))
        arrived = []
        cluster.nodes["C"].on_event = arrived.append
        for event in EVENTS:
            cluster.store("A").append(event)
        await cluster.converge()
        assert sorted(e.id for e in arrived) == sorted(e.id for e in EVENTS)

    run(scenario())


# --- Robustez -------------------------------------------------------------------------------


def test_garbage_and_invalid_events_are_dropped_without_crashing() -> None:
    async def scenario() -> None:
        cluster = Cluster(IDEAL, topology=("AB",))
        b = cluster.nodes["B"]
        await b.handle("A", b"")
        await b.handle("A", b"\xff\x00basura")
        await b.handle("A", b"\x12" + bytes(5))  # fragmento roto
        not_in_catalog = EVENTS[0].model_copy(update={"type": "inventado"})
        await b.handle("A", encode_event(not_in_catalog))
        bad_payload = EVENTS[0].model_copy(update={"seq": 99, "payload": {"x": 1}})
        await b.handle("A", encode_event(bad_payload))
        assert b.stats.garbage == 3
        assert b.stats.invalid == 2
        assert len(b.store.since({})) == 0

    run(scenario())


def test_vector_from_unknown_node_is_ignored() -> None:
    async def scenario() -> None:
        cluster = Cluster(IDEAL, topology=("AB",))
        cluster.store("B").append(EVENTS[0])
        await cluster.nodes["B"].handle("Z", encode_vector({}, "00" * 32))
        assert cluster.nodes["B"].stats.events_sent == 0

    run(scenario())


def test_node_cannot_be_its_own_peer() -> None:
    network = SimulatedNetwork(time_scale=0)
    with pytest.raises(ValueError):
        GossipNode("A", InMemoryEventStore(), network.endpoint("A"), ["A"])


def test_run_loop_syncs_and_stops() -> None:
    async def scenario() -> None:
        cluster = Cluster(IDEAL, topology=("AB",))
        cluster.store("A").append(EVENTS[0])
        stop = asyncio.Event()
        tasks = [
            asyncio.create_task(node.run(interval=0.02, stop=stop))
            for node in cluster.nodes.values()
        ]
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(cluster.digests()) == 1:
                break
        stop.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        assert len(cluster.digests()) == 1

    run(scenario())
