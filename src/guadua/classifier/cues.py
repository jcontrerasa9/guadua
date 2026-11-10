"""Señales de lenguaje que el clasificador (bolsa de palabras) no entiende. docs/diseno.md §11.

El clasificador ve "herida abierta" y dice alta, aunque el herido sea un perro; ve "fuga de
gas" y dice alta, aunque quien escribe dude ("creo que…, o no sé si es la basura"). Estas
señales no cambian el nivel: mandan el caso a la zona gris para que lo revise el agente de
triaje (N8) con un LLM, y sin LLM, una persona. La recepción (agents/recepcion.py) usa las
mismas para no preguntar por personas cuando el reporte es de un animal.

Se comparan sin tildes y en minúsculas (`rules.fold`). Ninguna puede bajar el piso: si una
regla vital se activó, el caso no entra a la zona gris (classifier/triage.py).
"""

from __future__ import annotations

import re

from guadua.classifier.rules import fold

ANIMAL = re.compile(
    r"\b(?:gat[oa]s?|gatic[oa]s?|perr[oa]s?|perrit[oa]s?|mascotas?|animal(?:es|ito)?|"
    r"caballos?|vacas?|gallinas?|pajar(?:o|os|ito)|loros?)\b"
)
PERSON = re.compile(
    r"\b(?:personas?|gente|senor(?:a|es|as)?|nin[oa]s?|bebes?|hombres?|mujer(?:es)?|"
    r"abuel[oa]s?|vecin[oa]s?|familia|hij[oa]s?|mama|papa|alguien|muert[oa]s?)\b"
)
# Duda de quien escribe. "Dicen que" / "me dijeron" no van aquí: eso es segunda mano, y ya
# lo marcan la cabecera ("Me contaron") y la correlación (rumor).
DOUBT = re.compile(
    r"\bcreo que\b|\bno se si\b|\bno estoy segur|\bparece que\b|\bparece ser\b|\btal vez\b|"
    r"\bquizas?\b|\ba lo mejor\b|\bpuede ser\b|\bno se bien\b"
)

ANIMAL_ONLY = "solo animales"
DOUBT_CUE = "duda en el texto"


def about_animal_only(text: str | None) -> bool:
    """El texto habla de un animal y no de personas ("hay un gato herido")."""
    folded = fold(text or "")
    return bool(ANIMAL.search(folded)) and not PERSON.search(folded)


def expresses_doubt(text: str | None) -> bool:
    """Quien escribe no está seguro de lo que reporta ("creo que", "no sé si")."""
    return bool(DOUBT.search(fold(text or "")))


def language_cues(text: str | None) -> tuple[str, ...]:
    """Señales del texto que piden revisión del agente de triaje, en orden fijo."""
    cues: list[str] = []
    if about_animal_only(text):
        cues.append(ANIMAL_ONLY)
    if expresses_doubt(text):
        cues.append(DOUBT_CUE)
    return tuple(cues)
