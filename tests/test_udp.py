"""Pruebas del transporte UDP y de la configuración del nodo."""

import asyncio

import pytest

from guadua.channel.profiles import IDEAL
from guadua.node.config import ConfigError, NodeConfig
from guadua.node.udp import UdpTransport
from guadua.providers.transport import Transport
from guadua.store.gossip import GossipNode
from tests.fakes import InMemoryEventStore
from tests.test_gossip import EVENTS

LOCAL = ("127.0.0.1", 0)  # puerto 0: el sistema elige uno libre


async def pair(**kwargs) -> tuple[UdpTransport, UdpTransport]:
    a = UdpTransport("A", LOCAL, {}, **kwargs)
    b = UdpTransport("B", LOCAL, {})
    await a.start()
    await b.start()
    a.peers["B"], b.peers["A"] = b.listen, a.listen
    return a, b


def test_round_trip_returns_origin() -> None:
    async def scenario() -> None:
        a, b = await pair()
        assert isinstance(a, Transport)
        await a.send("B", b"hola")
        assert await b.receive(timeout=1) == ("A", b"hola")
        assert await b.receive(timeout=0.05) is None
        a.close(), b.close()

    asyncio.run(scenario())


def test_mtu_is_enforced_and_unknown_peer_is_silent() -> None:
    async def scenario() -> None:
        a, b = await pair(mtu=21)
        with pytest.raises(ValueError):
            await a.send("B", b"x" * 22)
        await a.send("Z", b"a nadie")  # sin dirección: como un enlace caído
        await a.send("B", b"x" * 21)
        assert await b.receive(timeout=1) == ("A", b"x" * 21)
        a.close(), b.close()

    asyncio.run(scenario())


def test_fault_profile_drops_frames() -> None:
    async def scenario() -> None:
        a, b = await pair(profile=IDEAL.with_loss(1.0))
        await a.send("B", b"se pierde")
        assert await b.receive(timeout=0.1) is None
        a.close(), b.close()

    asyncio.run(scenario())


def test_garbage_datagram_is_ignored() -> None:
    async def scenario() -> None:
        a, b = await pair()
        a._transport.sendto(b"\x09AB", b.listen)  # dice 9 B de nombre y trae 2
        a._transport.sendto(b"", b.listen)
        await a.send("B", b"bien")
        assert await b.receive(timeout=1) == ("A", b"bien")
        a.close(), b.close()

    asyncio.run(scenario())


def test_gossip_converges_over_udp() -> None:
    async def scenario() -> None:
        a, b = await pair()
        nodes = [
            GossipNode("A", InMemoryEventStore(), a, ["B"]),
            GossipNode("B", InMemoryEventStore(), b, ["A"]),
        ]
        for event in EVENTS:
            nodes[0].store.append(event)
        for _ in range(20):
            for node in nodes:
                await node.tick()
            await asyncio.sleep(0.02)
            for node in nodes:
                await node.drain()
            if nodes[0].store.digest() == nodes[1].store.digest():
                break
        assert nodes[0].store.digest() == nodes[1].store.digest()
        assert nodes[1].peers_alive() == ("A",)
        a.close(), b.close()

    asyncio.run(scenario())


# --- Configuración --------------------------------------------------------------------------


def test_defaults_need_no_env() -> None:
    config = NodeConfig.from_env({})
    assert config.node_id == "A" and config.peers == () and config.db_path == ":memory:"


def test_reads_compose_style_env() -> None:
    config = NodeConfig.from_env(
        {"SARD_NODE_ID": "B", "SARD_PEERS": "A, C",
         "SARD_PEER_ADDRS": "A=node-a:9000,C=node-c:9001",
         "SARD_CHANNEL_PROFILE": "", "SARD_GOSSIP_INTERVAL": "0.5"}
    )  # fmt: skip
    assert config.peers == ("A", "C")
    assert config.peer_addrs == {"A": ("node-a", 9000), "C": ("node-c", 9001)}
    assert config.channel_profile is None and config.gossip_interval == 0.5


@pytest.mark.parametrize(
    "env",
    [
        {"SARD_NODE_ID": "A-1"},
        {"SARD_PEERS": "A"},
        {"SARD_HTTP_PORT": "ocho mil"},
        {"SARD_PEER_ADDRS": "B=sin-puerto"},
        {"SARD_PEERS": "B,C", "SARD_PEER_ADDRS": "B=node-b:9000"},
    ],
)
def test_bad_env_is_rejected(env: dict) -> None:
    with pytest.raises(ConfigError):
        NodeConfig.from_env(env)
