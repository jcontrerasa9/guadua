import json
from pathlib import Path

import pytest

from guadua.protocol.header import HEADER_SIZE, Header, decode, encode
from guadua.protocol.text import (
    DICT_VERSION,
    MAX_TEXT_BYTES,
    TextError,
    TextMode,
    decode_text,
    dictionary,
    dictionary_sha256,
    encode_text,
)
from guadua.protocol.text_savings import format_table, load_corpus, measure

FIXTURES = Path(__file__).parent / "fixtures"
VECTORS = json.loads((FIXTURES / "text_vectors.json").read_text(encoding="utf-8"))
SMS_BYTES = 140


def test_dictionary_matches_vectors() -> None:
    assert VECTORS["dict_version"] == DICT_VERSION
    assert VECTORS["max_text_bytes"] == MAX_TEXT_BYTES
    assert VECTORS["dict_sha256"] == dictionary_sha256()


def test_dictionary_fits_zlib_window() -> None:
    assert 0 < len(dictionary()) <= 32 * 1024
    dictionary().decode("utf-8")


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["name"])
def test_decode_matches_vector(case: dict) -> None:
    assert decode_text(bytes.fromhex(case["hex"])) == case["text"]


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["name"])
def test_encode_matches_vector(case: dict) -> None:
    encoded = encode_text(case["text"])
    assert encoded.hex() == case["hex"]
    assert len(encoded) == case["bytes"]
    assert (encoded[0] & 0x0F if encoded else None) == case["mode"]


@pytest.mark.parametrize("case", VECTORS["invalid_decode"], ids=lambda c: c["name"])
def test_decode_rejects(case: dict) -> None:
    with pytest.raises(TextError):
        decode_text(bytes.fromhex(case["hex"]))


@pytest.mark.parametrize("case", VECTORS["invalid_encode"], ids=lambda c: c["name"])
def test_encode_rejects(case: dict) -> None:
    with pytest.raises(TextError):
        encode_text(case["text"])


def test_vectors_cover_both_modes() -> None:
    modes = {c["mode"] for c in VECTORS["valid"]}
    assert {None, TextMode.PLANO, TextMode.DICCIONARIO} <= modes


def test_empty_text_adds_no_bytes() -> None:
    assert encode_text("") == b""
    assert decode_text(b"") == ""


def test_prefix_built_by_hand() -> None:
    """Modo plano armado a mano: prefijo 0x10 (versión 1, modo 0) + UTF-8."""
    assert decode_text(bytes.fromhex("10") + "sí".encode()) == "sí"
    assert encode_text("sí") == bytes.fromhex("10") + "sí".encode()


@pytest.mark.parametrize(
    "text",
    ["á é í ó ú ñ Ñ ü", "🆘 ayuda 🚑", "a", "Milán", "cra. 23 # 30-15", "\n\t "],
)
def test_round_trip(text: str) -> None:
    assert decode_text(encode_text(text)) == text


@pytest.mark.parametrize("text", load_corpus())
def test_never_grows_more_than_one_byte(text: str) -> None:
    assert len(encode_text(text)) <= len(text.encode("utf-8")) + 1


def test_encode_rejects_non_str() -> None:
    with pytest.raises(TextError):
        encode_text(b"bytes")  # type: ignore[arg-type]


def test_rejects_decompression_bomb() -> None:
    """Cien bytes que se inflan sin control no deben agotar la memoria del nodo."""
    import zlib

    comp = zlib.compressobj(9, zlib.DEFLATED, -15, zdict=dictionary())
    bomb = comp.compress(b"a" * 20_000) + comp.flush()
    data = bytes([(DICT_VERSION << 4) | TextMode.DICCIONARIO]) + bomb
    assert len(data) <= MAX_TEXT_BYTES
    with pytest.raises(TextError):
        decode_text(data)


def test_header_plus_text() -> None:
    header = Header(0, 0xA1B2C3D4, 7, 3600, 1, 1, 0x61, geohash="d2fkcdc")
    text = "colapso casa 3 atrapados huele a gas"
    message = encode(header) + encode_text(text)
    assert decode(message) == header
    assert decode_text(message[HEADER_SIZE:]) == text


def test_savings_measured_against_plain() -> None:
    """Terminado cuando de N1: el ahorro del diccionario frente a no comprimir se mide."""
    summary = measure(load_corpus())
    assert len(summary.rows) >= 20
    assert summary.savings >= 0.30
    assert summary.total_dictionary < summary.total_zlib_plain
    assert summary.mean_message < SMS_BYTES
    assert all(HEADER_SIZE + r.dictionary <= SMS_BYTES for r in summary.rows)
    table = format_table(summary)
    assert "Ahorro con diccionario" in table


def test_measure_rejects_empty_corpus() -> None:
    with pytest.raises(ValueError):
        measure([])
