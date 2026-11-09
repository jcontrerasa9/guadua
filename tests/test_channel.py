import asyncio
import json
import random

import pytest

from guadua.channel.faults import FaultModel
from guadua.channel.log import ChannelLog, describe_frame
from guadua.channel.profiles import (
    IDEAL,
    LORA,
    PROFILES,
    SMS,
    WIFI,
    ChannelError,
    ChannelProfile,
    profile_by_name,
)
from guadua.channel.simulated import SimulatedNetwork
from guadua.protocol.fragments import (
    Ack,
    Reassembler,
    Sender,
    decode_ack,
    decode_fragment,
    encode_ack,
    encode_fragment,
    split,
)
from guadua.protocol.header import MessageType
from guadua.providers.transport import Transport

FRAMES = 5000
TOLERANCE = 0.05


def _run(coro):
    return asyncio.run(coro)


async def _blast(network: SimulatedNetwork, src: str, dst: str, n: int, size: int = 21) -> None:
    sender = network.endpoint(src)
    for i in range(n):
        await sender.send(dst, i.to_bytes(4, "big") + b"x" * (size - 4))


# --- Perfiles -------------------------------------------------------------------------------


def test_profiles_match_design_frames() -> None:
    assert (WIFI.mtu, LORA.mtu, SMS.mtu) == (1200, 200, 140)
    assert profile_by_name("lora") is LORA
    with pytest.raises(ChannelError):
        profile_by_name("satelite")


@pytest.mark.parametrize(
    "changes",
    [
        {"loss": 1.5},
        {"loss": -0.1},
        {"duplicate": True},
        {"delay_min": 5, "delay_max": 1},
        {"cut_length": 0.5},
        {"mtu": 0},
        {"perdida": 0.3},  # campo desconocido
    ],
)
def test_profile_rejects_invalid(changes: dict) -> None:
    with pytest.raises(ChannelError):
        LORA.with_changes(**changes)


def test_expected_loss_combines_cuts_and_loss() -> None:
    p = ChannelProfile("x", 100, loss=0.5, delay_min=0, delay_max=0, duplicate=0,
                       cut_rate=0.1, cut_length=10)  # fmt: skip
    assert p.cut_fraction == pytest.approx(0.5)
    assert p.expected_loss == pytest.approx(0.75)
    assert IDEAL.expected_loss == 0


# --- Transport ------------------------------------------------------------------------------


def test_simulated_transport_fulfils_transport() -> None:
    async def scenario() -> None:
        network = SimulatedNetwork(seed=1, time_scale=0)
        network.link("A", "B", IDEAL.with_changes(mtu=21))
        a, b = network.endpoint("A"), network.endpoint("B")
        assert isinstance(a, Transport)
        assert a.mtu == 21
        await a.send("B", b"x" * 21)
        assert await b.receive(timeout=0.1) == ("A", b"x" * 21)  # devuelve el origen
        assert await b.receive(timeout=0.01) is None
        with pytest.raises(ValueError):
            await a.send("B", b"x" * 22)

    _run(scenario())


def test_receive_waits_for_delay_in_real_time() -> None:
    async def scenario() -> None:
        network = SimulatedNetwork(seed=1, time_scale=1)
        network.link("A", "B", IDEAL.with_changes(delay_min=0.05, delay_max=0.05))
        await network.endpoint("A").send("B", b"hola")
        b = network.endpoint("B")
        assert await b.receive(timeout=0.01) is None  # todavía viaja
        assert await b.receive(timeout=0.5) == ("A", b"hola")

    _run(scenario())


# --- Terminado cuando de N3 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "profile",
    [WIFI, LORA, SMS, LORA.with_loss(0.6), WIFI.with_loss(0.0).with_changes(cut_rate=0)],
    ids=["wifi", "lora_30", "sms", "lora_60", "wifi_sin_perdida"],
)
def test_measured_loss_matches_configured(profile: ChannelProfile) -> None:
    """Con semilla fija, la pérdida medida coincide con la configurada (±5 %)."""

    async def scenario() -> tuple[int, float]:
        network = SimulatedNetwork(seed=42, time_scale=0)
        network.link("A", "B", profile)
        await _blast(network, "A", "B", FRAMES, size=min(profile.mtu, 21))
        b = network.endpoint("B")
        received = 0
        while await b.receive(timeout=0) is not None:
            received += 1
        return received, network.stats("A", "B")

    received, stats = _run(scenario())
    measured = stats.measured_loss
    assert abs(measured - profile.expected_loss) <= TOLERANCE, (
        f"{profile.name}: medida {measured:.1%}, configurada {profile.expected_loss:.1%}"
    )
    # Lo que llega a B cuadra con los contadores: no se pierde ni inventa nada fuera de ellos.
    assert stats.sent == FRAMES
    assert received == stats.sent - stats.lost + stats.duplicated == stats.delivered


def test_same_seed_same_outcome_other_seed_differs() -> None:
    def outcome(seed: int) -> list[bool]:
        model = FaultModel(LORA, random.Random(seed))
        return [model.decide().lost for _ in range(500)]

    assert outcome(7) == outcome(7)
    assert outcome(7) != outcome(8)


def test_links_are_independent_of_traffic_on_other_links() -> None:
    async def lost_on_ab(extra_traffic: bool) -> int:
        network = SimulatedNetwork(seed=3, time_scale=0)
        network.link("A", "B", LORA)
        network.link("B", "C", LORA)
        if extra_traffic:
            await _blast(network, "B", "C", 300)
        await _blast(network, "A", "B", 300)
        return network.stats("A", "B").lost

    assert _run(lost_on_ab(False)) == _run(lost_on_ab(True))


# --- Desorden, duplicados y cortes ----------------------------------------------------------


def test_sms_delivers_out_of_order() -> None:
    async def scenario() -> list[int]:
        network = SimulatedNetwork(seed=5, time_scale=0.001)  # 2–30 s → 2–30 ms
        network.link("A", "B", SMS.with_changes(loss=0, duplicate=0))
        await _blast(network, "A", "B", 40)
        b = network.endpoint("B")
        order = []
        while (item := await b.receive(timeout=0.1)) is not None:
            order.append(int.from_bytes(item[1][:4], "big"))
        return order

    order = _run(scenario())
    assert sorted(order) == list(range(40))
    assert order != list(range(40))


def test_duplicates_arrive_twice_and_are_counted() -> None:
    async def scenario() -> tuple[int, int]:
        network = SimulatedNetwork(seed=1, time_scale=0)
        network.link("A", "B", IDEAL.with_changes(duplicate=0.5))
        await _blast(network, "A", "B", 1000)
        b = network.endpoint("B")
        received = 0
        while await b.receive(timeout=0) is not None:
            received += 1
        return received, network.stats("A", "B").duplicated

    received, duplicated = _run(scenario())
    assert received == 1000 + duplicated
    assert 400 <= duplicated <= 600


def test_cuts_lose_consecutive_frames() -> None:
    profile = IDEAL.with_changes(cut_rate=0.02, cut_length=20)
    model = FaultModel(profile, random.Random(9))
    lost = [model.decide().lost for _ in range(3000)]
    longest = max(len(run) for run in "".join("x" if x else "." for x in lost).split("."))
    assert longest >= 10
    assert model.stats.lost == model.stats.lost_in_cut  # sin pérdida suelta, todo es corte


# --- Topología, particiones y control en vivo ----------------------------------------------


def test_topology_a_b_c_and_partition() -> None:
    async def scenario() -> None:
        network = SimulatedNetwork(seed=1, time_scale=0)
        network.link("A", "B", IDEAL)
        network.link("B", "C", IDEAL)
        a, b, c = (network.endpoint(n) for n in "ABC")
        await a.send("C", b"directo")  # no hay enlace A–C
        assert await c.receive(timeout=0) is None
        await a.send("B", b"salto")
        _, frame = await b.receive(timeout=0)
        await b.send("C", frame)
        assert await c.receive(timeout=0) == ("B", b"salto")

        network.disconnect("B", "C")
        await b.send("C", b"particion")
        assert await c.receive(timeout=0) is None
        network.connect("B", "C")
        await b.send("C", b"de vuelta")
        assert await c.receive(timeout=0) == ("B", b"de vuelta")
        kinds = [e.kind for e in network.log.events()]
        assert kinds.count("sin_enlace") == 2

    _run(scenario())


def test_live_update_changes_measured_loss() -> None:
    async def scenario() -> tuple[float, float]:
        network = SimulatedNetwork(seed=11, time_scale=0)
        network.link("A", "B", LORA.with_changes(cut_rate=0))
        await _blast(network, "A", "B", FRAMES)
        first = network.stats("A", "B").measured_loss
        before = network.stats("A", "B").lost
        network.update("A", "B", loss=0.6)
        assert network.profile("B", "A").loss == 0.6  # ambos sentidos
        await _blast(network, "A", "B", FRAMES)
        second = (network.stats("A", "B").lost - before) / FRAMES
        return first, second

    first, second = _run(scenario())
    assert abs(first - 0.3) <= TOLERANCE
    assert abs(second - 0.6) <= TOLERANCE


def test_live_controls_reject_unknown_link_and_bad_values() -> None:
    network = SimulatedNetwork(seed=1, time_scale=0)
    network.link("A", "B", LORA)
    with pytest.raises(ChannelError):
        network.update("A", "C", loss=0.1)
    with pytest.raises(ChannelError):
        network.update("A", "B", loss=2)
    assert network.profile("A", "B") == LORA  # un cambio inválido no deja nada a medias
    with pytest.raises(ChannelError):
        network.link("A", "A", LORA)


# --- Registro y resumen para la demo -------------------------------------------------------


def test_describe_frame_reads_message_type() -> None:
    fragments = split(b"x" * 400, device_id=1, seq=1, mtu=200, msg_type=MessageType.FRAGMENTO_FOTO)
    assert describe_frame(encode_fragment(fragments[2])) == "fragmento foto 3/3"
    ack = Ack(MessageType.FRAGMENTO_FOTO, 1, 1, 17, frozenset(range(5)))
    assert describe_frame(encode_ack(ack)) == "ACK fragmento foto (5 de 17)"
    assert describe_frame(bytes.fromhex("11") + bytes(20)) == "SOS escombros"
    assert describe_frame(bytes.fromhex("10") + bytes(20)) == "reporte"
    assert describe_frame(b"") == "desconocido"
    assert describe_frame(bytes.fromhex("1f")) == "tipo 15"
    assert describe_frame(bytes.fromhex("12")) == "fragmento inválido"


def test_log_records_in_order_is_bounded_and_notifies() -> None:
    async def scenario() -> None:
        log = ChannelLog(capacity=50)
        seen = []
        unsubscribe = log.subscribe(seen.append)
        network = SimulatedNetwork(seed=2, time_scale=0, log=log)
        network.link("A", "B", LORA)
        await _blast(network, "A", "B", 200)
        assert len(log) == 50
        assert len(seen) >= 200
        assert {e.kind for e in seen} >= {"entregada", "perdida"}
        assert all(e.link == "A→B" and e.profile == "lora" for e in seen)
        unsubscribe()
        network.update("A", "B", loss=0.5)
        assert seen[-1].kind != "cambio"
        assert log.events(1)[0].kind == "cambio"
        assert "loss=0.5" in log.events(1)[0].description
        assert "lora" in str(log.events(1)[0])

    _run(scenario())


def test_snapshot_is_json_and_counters_add_up() -> None:
    async def scenario() -> dict:
        network = SimulatedNetwork(seed=4, time_scale=0)
        network.link("A", "B", WIFI)
        await _blast(network, "A", "B", 500)
        network.disconnect("A", "B")
        return network.snapshot()

    snapshot = json.loads(json.dumps(_run(scenario())))
    ab = next(link for link in snapshot["links"] if link["link"] == "A→B")
    stats = ab["stats"]
    assert ab["connected"] is False
    assert ab["profile"]["name"] == "wifi"
    assert ab["profile"]["expected_loss"] == pytest.approx(WIFI.expected_loss, abs=1e-4)
    assert stats["sent"] == 500
    assert stats["delivered"] == stats["sent"] - stats["lost"] + stats["duplicated"]


def test_all_named_profiles_are_valid_and_serialisable() -> None:
    for profile in PROFILES.values():
        json.dumps(profile.as_dict())


# --- Junto con N2: la foto viaja por el canal real -----------------------------------------


@pytest.mark.parametrize("seed", range(5))
def test_photo_over_simulated_lora_at_60_percent(seed: int) -> None:
    async def scenario() -> bool:
        network = SimulatedNetwork(seed=seed, time_scale=0)
        network.link("A", "B", LORA.with_loss(0.6).with_changes(cut_rate=0))
        a, b = network.endpoint("A"), network.endpoint("B")
        photo = random.Random(seed).randbytes(3 * 1024)
        fragments = split(photo, device_id=1, seq=1, mtu=a.mtu, msg_type=MessageType.FRAGMENTO_FOTO)
        sender, receiver, result = Sender(fragments), Reassembler(), None
        for _ in range(200):
            if sender.done:
                break
            for fragment in sender.pending():
                await a.send("B", encode_fragment(fragment))
            while (item := await b.receive(timeout=0)) is not None:
                fragment = decode_fragment(item[1])
                result = receiver.add(fragment) or result
                await b.send("A", encode_ack(receiver.ack_for(fragment)))
            while (item := await a.receive(timeout=0)) is not None:
                sender.on_ack(decode_ack(item[1]))
        return sender.done and result == photo

    assert _run(scenario())
