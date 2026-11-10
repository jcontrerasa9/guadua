"""Ráfaga de reportes sintéticos para la barra del jurado (D9).

Arma mensajes binarios como los de la PWA (cabecera de 21 B + texto con diccionario) a
partir del conjunto de prueba de `data/sinteticos`, cada uno en un barrio de Manizales. El
proxy de fallos los envía al nodo pasando por el perfil del canal activo.
"""

from __future__ import annotations

import json
import random
from functools import cache
from pathlib import Path

from guadua.classifier.synthetic import Row, load
from guadua.protocol.header import Flag, Header, MessageType, People, encode
from guadua.protocol.text import TextError, encode_text

BARRIOS_PATH = Path(__file__).resolve().parents[3] / "data" / "barrios.json"
MAX_BURST = 50


@cache
def _rows() -> tuple[Row, ...]:
    rows = []
    for row in load():
        if row.split != "test":
            continue
        try:
            encode_text(row.text)
        except TextError:
            continue
        rows.append(row)
    return tuple(rows)


@cache
def _barrios() -> tuple[int, ...]:
    try:
        codes = tuple(b["code"] for b in json.loads(BARRIOS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        codes = ()
    return codes or tuple(range(1, 11))


def burst_messages(count: int, rng: random.Random, device_id: int, first_seq: int) -> list[bytes]:
    """`count` reportes distintos del mismo "dispositivo" con consecutivos desde `first_seq`.
    La mitad, más o menos, dice haberlo visto en persona."""
    if not 1 <= count <= MAX_BURST:
        raise ValueError(f"la ráfaga va de 1 a {MAX_BURST} reportes")
    rows, barrios = _rows(), _barrios()
    out = []
    for i in range(count):
        row = rng.choice(rows)
        flags = Flag.OBSERVACION_DIRECTA if rng.random() < 0.5 else Flag(0)
        header = Header(
            MessageType.REPORTE,
            device_id,
            first_seq + i,
            3600,
            row.incident,
            People.NO_SE,
            flags,
            barrio=rng.choice(barrios),
        )
        out.append(encode(header) + encode_text(row.text))
    return out
