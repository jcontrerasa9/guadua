import json
from pathlib import Path

import pytest

from sard.protocol.header import HEADER_SIZE, Header, HeaderError, decode, encode

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "header_vectors.json").read_text())


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["name"])
def test_encode_matches_vector(case: dict) -> None:
    assert encode(Header(**case["fields"])).hex() == case["hex"]


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["name"])
def test_decode_matches_vector(case: dict) -> None:
    assert decode(bytes.fromhex(case["hex"])) == Header(**case["fields"])


@pytest.mark.parametrize("case", VECTORS["invalid_decode"], ids=lambda c: c["name"])
def test_decode_rejects(case: dict) -> None:
    with pytest.raises(HeaderError):
        decode(bytes.fromhex(case["hex"]))


@pytest.mark.parametrize("case", VECTORS["invalid_encode"], ids=lambda c: c["name"])
def test_encode_rejects(case: dict) -> None:
    with pytest.raises(HeaderError):
        encode(Header(**case["fields"]))


def test_sos_built_by_hand() -> None:
    """Vector armado a mano, campo por campo, independiente del códec."""
    expected = bytes.fromhex(
        "11"  # versión 1, tipo 1 (SOS escombros)
        "a1b2c3d4"  # dispositivo
        "00000007"  # consecutivo 7
        "00015180"  # 86400 s desde 2026-01-01
        "00"  # incidente: atrapados
        "00"  # personas: una
        "e1"  # banderas: atrapados, observación directa, GPS, batería crítica
        "609d25b160"  # geohash d2fkcdc (35 bits) + 5 bits en cero
    )
    assert len(expected) == HEADER_SIZE
    assert encode(decode(expected)) == expected
    assert decode(expected).geohash == "d2fkcdc"


def test_trailing_text_is_ignored() -> None:
    raw = bytes.fromhex(VECTORS["valid"][0]["hex"])
    assert decode(raw + b"texto") == decode(raw)
