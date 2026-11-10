"""Señales de lenguaje para la zona gris del triaje (classifier/cues.py)."""

import pytest

from guadua.classifier.cues import (
    ANIMAL_ONLY,
    DOUBT_CUE,
    about_animal_only,
    expresses_doubt,
    language_cues,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hay un perro que tiene una herida abierta", True),
        ("un GATICO atrapado en el techo", True),
        ("el perro y el señor están heridos", False),  # hay una persona
        ("mi vecino está herido", False),
        (None, False),
    ],
)
def test_solo_animales(text, expected) -> None:
    assert about_animal_only(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Creo que hay una fuga de gas", True),
        ("no sé si es la basura", True),  # con tilde
        ("Tal vez se cayó el muro", True),
        ("hay fuga de gas", False),
        ("dicen que hay heridos", False),  # segunda mano, no duda
    ],
)
def test_duda(text, expected) -> None:
    assert expresses_doubt(text) is expected


def test_language_cues_en_orden_fijo() -> None:
    assert language_cues("creo que el perro está herido") == (ANIMAL_ONLY, DOUBT_CUE)
    assert language_cues("casa colapsada") == ()
