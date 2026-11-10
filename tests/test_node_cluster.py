"""Tres nodos A ↔ B ↔ C en un mismo programa, sobre el canal simulado (N3) y el gossip (N5)."""

import asyncio

from guadua.channel.profiles import IDEAL, LORA
from guadua.channel.simulated import SimulatedNetwork
from guadua.node.api import DecisionRequest
from guadua.node.service import Node
from guadua.node.views import incident_cards
from guadua.protocol.header import Flag, Header, encode
from guadua.protocol.text import encode_text
from guadua.store.sqlite import SQLiteEventStore

REPORT = encode(
    Header(0, 0xA1B2C3D4, 7, 3600, 1, 1, Flag.ATRAPADOS | Flag.GPS, geohash="d2fkcdc")
) + encode_text("colapso casa 3 atrapados")


def cluster(profile=IDEAL, seed=0) -> dict[str, Node]:
    network = SimulatedNetwork(seed=seed, time_scale=0)
    network.link("A", "B", profile)
    network.link("B", "C", profile)
    peers = {"A": ("B",), "B": ("A", "C"), "C": ("B",)}
    return {
        n: Node(n, SQLiteEventStore(), transport=network.endpoint(n), peers=peers[n],
                clock=lambda: 1_794_573_600.0)
        for n in "ABC"
    }  # fmt: skip


async def sync(nodes: dict[str, Node], max_rounds: int = 400) -> int:
    for rounds in range(1, max_rounds + 1):
        for node in nodes.values():
            await node.gossip.tick()
        while sum([await node.gossip.drain() for node in nodes.values()]):
            pass
        if len({node.store.digest() for node in nodes.values()}) == 1:
            return rounds
    raise AssertionError("no convergió")


def test_report_through_a_shows_on_c_and_only_a_derives() -> None:
    async def scenario() -> None:
        nodes = cluster()
        nodes["A"].receive_message(REPORT)
        await sync(nodes)
        (card,) = incident_cards(nodes["C"].store.since({}))
        assert card.incident_id == "a1b2c3d4-7" and card.status == "pendiente"
        derived = [e for e in nodes["C"].store.since({}) if e.category == "derivado"]
        assert derived and {e.origin_node for e in derived} == {"A"}  # solo el dueño deriva
        assert all(e.origin_node == "A" for e in nodes["B"].store.since({}))

    asyncio.run(scenario())


def test_rejection_on_c_makes_owner_a_replan() -> None:
    async def scenario() -> None:
        nodes = cluster()
        nodes["A"].receive_message(REPORT)
        await sync(nodes)
        (card,) = incident_cards(nodes["C"].store.since({}))
        nodes["C"].decide(
            DecisionRequest(ficha_id=card.ficha_id, action="rechazar", operator="comandante")
        )
        await sync(nodes)
        (after,) = incident_cards(nodes["C"].store.since({}))
        assert after.ficha_id != card.ficha_id and after.status == "pendiente"
        new_ficha = after.ficha_id.split(":")[0]
        assert new_ficha == "A"  # la replaneó el dueño de la ficha, no C
        assert "replaneada" in after.suggestion  # y en C se ve que fue replaneada

    asyncio.run(scenario())


def test_converges_with_60_percent_loss() -> None:
    async def scenario() -> None:
        nodes = cluster(LORA.with_loss(0.6), seed=2)
        nodes["A"].receive_message(REPORT)
        await sync(nodes)
        assert incident_cards(nodes["C"].store.since({}))

    asyncio.run(scenario())


def test_duplicate_report_at_another_node_after_sync() -> None:
    """Si la PWA reintenta por otro nodo, ese nodo ya conoce el reporte por gossip."""

    async def scenario() -> None:
        nodes = cluster()
        nodes["A"].receive_message(REPORT)
        await sync(nodes)
        assert nodes["C"].receive_message(REPORT).duplicate is True

    asyncio.run(scenario())
