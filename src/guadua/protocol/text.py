"""Texto opcional del reporte, comprimido con diccionario. CONTRATO: ver docs/contratos.md.

Va después de los 21 B de la cabecera y ocupa como máximo 100 B:

- 0 B: sin texto (el SOS escombros sigue pesando 21 B).
- 1 B de prefijo + datos. 4 bits altos: versión del diccionario (hoy 1).
  4 bits bajos: modo. 0 = UTF-8 sin comprimir; 1 = deflate crudo (sin encabezado ni suma
  de control de zlib) con el diccionario de Manizales como `zdict`.

El codificador usa el modo 1 solo si ocupa menos que el modo 0, así que en el peor caso el
texto crece 1 B. El diccionario son los bytes exactos de `data/diccionario_manizales.txt`;
cambiarlo exige subir DICT_VERSION. Debe decodificar igual que el códec JS (N16) con
`tests/fixtures/text_vectors.json`.
"""

from __future__ import annotations

import hashlib
import zlib
from enum import IntEnum
from functools import cache
from pathlib import Path

MAX_TEXT_BYTES = 100
DICT_VERSION = 1
DICT_PATH = Path(__file__).resolve().parents[3] / "data" / "diccionario_manizales.txt"
_WBITS = -15  # deflate crudo: ahorra los 2 B de encabezado y los 4 B de Adler-32 de zlib
_LEVEL = 9


class TextMode(IntEnum):
    PLANO = 0
    DICCIONARIO = 1


class TextError(ValueError):
    """Texto inválido al codificar o decodificar."""


@cache
def dictionary() -> bytes:
    """Bytes del diccionario, tal cual están en el archivo."""
    return DICT_PATH.read_bytes()


def dictionary_sha256() -> str:
    return hashlib.sha256(dictionary()).hexdigest()


def _compress(data: bytes) -> bytes:
    comp = zlib.compressobj(_LEVEL, zlib.DEFLATED, _WBITS, zdict=dictionary())
    return comp.compress(data) + comp.flush()


def _decompress(data: bytes) -> bytes:
    decomp = zlib.decompressobj(_WBITS, zdict=dictionary())
    try:
        out = decomp.decompress(data, MAX_TEXT_BYTES * 20)
    except zlib.error as exc:
        raise TextError(f"deflate inválido: {exc}") from exc
    if not decomp.eof or decomp.unused_data or decomp.unconsumed_tail:
        raise TextError("deflate incompleto o con bytes de sobra")
    return out


def encode_text(text: str) -> bytes:
    """Devuelve los bytes que van después de la cabecera (como máximo 100 B)."""
    if not isinstance(text, str):
        raise TextError(f"se esperaba str: {text!r}")
    if not text:
        return b""
    raw = text.encode("utf-8")
    packed = _compress(raw)
    mode, body = (TextMode.DICCIONARIO, packed) if len(packed) < len(raw) else (TextMode.PLANO, raw)
    out = bytes([(DICT_VERSION << 4) | mode]) + body
    if len(out) > MAX_TEXT_BYTES:
        raise TextError(f"el texto ocupa {len(out)} B; el máximo es {MAX_TEXT_BYTES} B")
    return out


def decode_text(data: bytes) -> str:
    """Interpreta los bytes que siguen a la cabecera. Sin bytes, el texto es vacío."""
    if not data:
        return ""
    if len(data) > MAX_TEXT_BYTES:
        raise TextError(f"llegaron {len(data)} B de texto; el máximo es {MAX_TEXT_BYTES} B")
    version, mode = data[0] >> 4, data[0] & 0x0F
    if version != DICT_VERSION:
        raise TextError(f"versión de diccionario no soportada: {version}")
    if mode == TextMode.PLANO:
        raw = data[1:]
    elif mode == TextMode.DICCIONARIO:
        raw = _decompress(data[1:])
    else:
        raise TextError(f"modo de texto desconocido: {mode}")
    if not raw:
        raise TextError("prefijo sin texto")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TextError("el texto no es UTF-8 válido") from exc
