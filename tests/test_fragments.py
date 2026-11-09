import json
import random
from pathlib import Path

import pytest

from guadua.protocol.fragments import (
    ACK_HEADER_SIZE,
    FRAGMENT_HEADER_SIZE,
    MAX_FRAGMENTS,
    Ack,
    Fragment,
    FragmentError,
    Reassembler,
    Sender,
    decode_ack,
    decode_fragment,
    encode_ack,
    encode_fragment,
    split,
)
from guadua.protocol.header import MessageType

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "fragment_vectors.json").read_text())
LORA_MTU = 200
FOTO = MessageType.FRAGMENTO_FOTO


def _fragment(fields: dict) -> Fragment:
    return Fragment(**{**fields, "data": bytes.fromhex(fields["data"])})


def _ack(fields: dict) -> Ack:
    return Ack(**{**fields, "received": frozenset(fields["received"])})


def test_sizes_match_vectors() -> None:
    assert VECTORS["fragment_header_size"] == FRAGMENT_HEADER_SIZE
    assert VECTORS["ack_header_size"] == ACK_HEADER_SIZE
    assert VECTORS["max_fragments"] == MAX_FRAGMENTS


# --- Vectores compartidos con JS ---------------------------------------------------------


@pytest.mark.parametrize("case", VECTORS["fragments"], ids=lambda c: c["name"])
def test_fragment_vectors(case: dict) -> None:
    assert encode_fragment(_fragment(case["fields"])).hex() == case["hex"]
    assert decode_fragment(bytes.fromhex(case["hex"])) == _fragment(case["fields"])


@pytest.mark.parametrize("case", VECTORS["acks"], ids=lambda c: c["name"])
def test_ack_vectors(case: dict) -> None:
    assert encode_ack(_ack(case["fields"])).hex() == case["hex"]
    assert decode_ack(bytes.fromhex(case["hex"])) == _ack(case["fields"])


@pytest.mark.parametrize("case", VECTORS["invalid_fragment_decode"], ids=lambda c: c["name"])
def test_fragment_decode_rejects(case: dict) -> None:
    with pytest.raises(FragmentError):
        decode_fragment(bytes.fromhex(case["hex"]))


@pytest.mark.parametrize("case", VECTORS["invalid_ack_decode"], ids=lambda c: c["name"])
def test_ack_decode_rejects(case: dict) -> None:
    with pytest.raises(FragmentError):
        decode_ack(bytes.fromhex(case["hex"]))


def test_ack_built_by_hand() -> None:
    """ACK armado a mano, campo por campo, independiente del códec."""
    raw = bytes.fromhex(
        "13"  # versión 1, tipo 3 (ACK)
        "0000002a"  # dispositivo 42
        "00000003"  # consecutivo 3
        "08"  # confirma fragmentos de foto
        "09"  # 9 fragmentos
        "81"  # byte 0: fragmentos 0 y 7
        "01"  # byte 1: fragmento 8
    )
    ack = decode_ack(raw)
    assert ack.received == {0, 7, 8}
    assert ack.missing == [1, 2, 3, 4, 5, 6]
    assert encode_ack(ack) == raw


# --- Validación al codificar ---------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"msg_type": MessageType.REPORTE},
        {"index": 3},
        {"total": 0, "index": 0},
        {"data": b""},
        {"device_id": 2**32},
        {"seq": -1},
    ],
)
def test_fragment_rejects(fields: dict) -> None:
    base = {"msg_type": 2, "device_id": 1, "seq": 1, "index": 0, "total": 3, "data": b"x"}
    with pytest.raises(FragmentError):
        Fragment(**{**base, **fields})


@pytest.mark.parametrize(
    "fields",
    [{"acked_type": MessageType.ACK}, {"received": frozenset({3})}, {"total": 256}],
)
def test_ack_rejects(fields: dict) -> None:
    base = {"acked_type": 2, "device_id": 1, "seq": 1, "total": 3, "received": frozenset()}
    with pytest.raises(FragmentError):
        Ack(**{**base, **fields})


# --- Partir y juntar ------------------------------------------------------------------------


@pytest.mark.parametrize("mtu", [140, 200, 1200])
@pytest.mark.parametrize("size", [1, 129, 130, 3072])
def test_split_and_reassemble_out_of_order(mtu: int, size: int) -> None:
    rng = random.Random(size * mtu)
    message = rng.randbytes(size)
    fragments = split(message, device_id=5, seq=9, mtu=mtu)
    assert all(len(encode_fragment(f)) <= mtu for f in fragments)
    shuffled = fragments + fragments[: len(fragments) // 2]  # con repetidos
    rng.shuffle(shuffled)
    receiver = Reassembler()
    delivered = [m for f in shuffled if (m := receiver.add(decode_fragment(encode_fragment(f))))]
    assert delivered == [message]  # se entrega una sola vez, intacto
    assert receiver.ack_for(fragments[0]).complete


def test_split_respects_limits() -> None:
    capacity = LORA_MTU - FRAGMENT_HEADER_SIZE
    assert len(split(b"x" * capacity * MAX_FRAGMENTS, device_id=1, seq=1, mtu=LORA_MTU)) == 255
    with pytest.raises(FragmentError):
        split(b"x" * (capacity * MAX_FRAGMENTS + 1), device_id=1, seq=1, mtu=LORA_MTU)
    with pytest.raises(FragmentError):
        split(b"", device_id=1, seq=1, mtu=LORA_MTU)
    with pytest.raises(FragmentError):
        split(b"x", device_id=1, seq=1, mtu=FRAGMENT_HEADER_SIZE)


def test_text_and_photo_with_same_id_do_not_mix() -> None:
    receiver = Reassembler()
    text = split(b"reporte", device_id=7, seq=1, mtu=LORA_MTU)
    photo = split(b"\xff" * 400, device_id=7, seq=1, mtu=LORA_MTU, msg_type=FOTO)
    assert receiver.add(text[0]) == b"reporte"
    assert receiver.add(photo[0]) is None
    assert receiver.ack(FOTO, 7, 1).received == {0}
    assert receiver.ack(MessageType.FRAGMENTO, 7, 1).complete


def test_duplicate_is_stored_once_and_conflicts_are_rejected() -> None:
    receiver = Reassembler()
    first, second = split(b"a" * 300, device_id=1, seq=1, mtu=LORA_MTU)
    assert receiver.add(first) is None
    assert receiver.add(first) is None
    assert receiver.ack_for(first).received == {0}
    with pytest.raises(FragmentError):
        receiver.add(Fragment(2, 1, 1, 0, 2, b"otro"))  # mismo índice, otros datos
    with pytest.raises(FragmentError):
        receiver.add(Fragment(2, 1, 1, 1, 3, b"a"))  # otro total
    assert receiver.add(second) == b"a" * 300
    assert receiver.add(second) is None  # repetido tras completar: no se entrega de nuevo


def test_reassembler_ack_unknown_message() -> None:
    with pytest.raises(FragmentError):
        Reassembler().ack(MessageType.FRAGMENTO, 1, 1)


def test_sender_rejects_foreign_ack_and_bad_input() -> None:
    sender = Sender(split(b"x" * 300, device_id=1, seq=1, mtu=LORA_MTU))
    with pytest.raises(FragmentError):
        sender.on_ack(Ack(2, 1, 2, 2, frozenset()))  # otro consecutivo
    with pytest.raises(FragmentError):
        Sender([])
    with pytest.raises(FragmentError):
        Sender(split(b"x" * 300, device_id=1, seq=1, mtu=LORA_MTU)[1:])


# --- Terminado cuando de N2 -----------------------------------------------------------------


def _lossy(rng: random.Random, loss: float, frame: bytes, stats: dict) -> bytes | None:
    """Canal mínimo con pérdida (N3 aún no existe)."""
    stats["frames"] += 1
    if rng.random() < loss:
        stats["lost"] += 1
        return None
    return frame


def _send_photo_over_lossy_link(seed: int, loss: float = 0.6) -> dict:
    """Envía una foto de 3 KB por LoRa; se pierden fragmentos y ACK por igual."""
    rng = random.Random(seed)
    photo = rng.randbytes(3 * 1024)
    fragments = split(photo, device_id=0xA1B2C3D4, seq=7, mtu=LORA_MTU, msg_type=FOTO)
    sender, receiver = Sender(fragments), Reassembler()
    stats = {"frames": 0, "lost": 0, "fragments_sent": 0, "rounds": 0, "total": len(fragments)}
    confirmed: set[int] = set()  # lo que algún ACK que llegó al emisor ya confirmó
    result = None
    while not sender.done and stats["rounds"] < 200:
        stats["rounds"] += 1
        for fragment in sender.pending():
            assert fragment.index not in confirmed, "reenvió un fragmento ya confirmado"
            stats["fragments_sent"] += 1
            frame = _lossy(rng, loss, encode_fragment(fragment), stats)
            if frame is not None:
                result = receiver.add(decode_fragment(frame)) or result
        ack_frame = _lossy(rng, loss, encode_ack(receiver.ack(FOTO, 0xA1B2C3D4, 7)), stats)
        if ack_frame is not None:
            ack = decode_ack(ack_frame)
            sender.on_ack(ack)
            confirmed |= ack.received
    stats["ok"] = sender.done and result == photo
    return stats


@pytest.mark.parametrize("seed", range(10))
def test_photo_3kb_survives_60_percent_loss_resending_only_missing(seed: int) -> None:
    """Terminado cuando de N2: foto de 3 KB, 60 % de pérdida, se reenvía solo lo que falta.

    Dentro de la simulación se verifica en cada ronda que ningún fragmento ya confirmado por
    un ACK se vuelva a enviar.
    """
    stats = _send_photo_over_lossy_link(seed)
    assert stats["total"] == 17
    assert stats["ok"], stats


def test_measured_loss_matches_configured_and_saves_resends() -> None:
    runs = [_send_photo_over_lossy_link(seed) for seed in range(100)]
    assert all(r["ok"] for r in runs)
    loss = sum(r["lost"] for r in runs) / sum(r["frames"] for r in runs)
    assert 0.55 <= loss <= 0.65, f"pérdida medida {loss:.1%}"
    # Reenviar todo en cada ronda costaría 17 fragmentos por ronda.
    selective = sum(r["fragments_sent"] for r in runs)
    resend_all = sum(r["total"] * r["rounds"] for r in runs)
    assert selective < 0.6 * resend_all
    print(
        f"pérdida {loss:.1%}; enviados {selective / 100:.1f} por foto frente a "
        f"{resend_all / 100:.1f} reenviando todo"
    )
