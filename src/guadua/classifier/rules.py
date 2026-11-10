"""Capa 1 del triaje: reglas de prioridad mínima (docs/diseno.md §11).

Las señales vitales fijan el piso en crítica: atrapados, no respira, sangrado fuerte,
inconsciente, incendio y SOS de escombros. Salen de la cabecera (banderas, tipo de mensaje
y de incidente) y del texto. Objetivo: menos de 1 ms por reporte.

Las reglas prefieren equivocarse hacia arriba: no interpretan negaciones ("no hay
atrapados" también sube a crítica). Bajar un caso es decisión de una persona, no del piso.

Única excepción: si el texto habla **solo de animales** (no nombra a ninguna persona) y la
cabecera no dice cuántas personas hay, las señales vitales de atrapados, no respira,
sangrado e inconsciente no fijan el piso ("hay un gato atrapado en un árbol"). El caso no
baja solo: queda en la zona gris para el agente de triaje y, sin LLM, para una persona. El
SOS de escombros, el incendio y la bandera "alguien no respira" siguen fijando el piso.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from guadua.classifier.urgency import Urgency
from guadua.protocol.header import Flag, Incident, MessageType, People
from guadua.store.event_types import Reporte

SOS = "sos escombros → crítica"
ATRAPADOS = "atrapados → crítica"
NO_RESPIRA_O_SANGRADO = "no respira o sangrado fuerte → crítica"  # bandera de la cabecera
NO_RESPIRA = "no respira → crítica"
SANGRADO = "sangrado fuerte → crítica"
INCONSCIENTE = "inconsciente → crítica"
INCENDIO = "incendio → crítica"

# El texto se compara sin tildes y en minúsculas (ver `fold`).
TEXT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        ATRAPADOS,
        re.compile(
            r"atrapad|atrapao|sepultad|enterrad|\b(?:debajo|bajo) (?:de )?(?:los |las |la |el )?"
            r"(?:escombro|placa|muro|pared|techo|loza|losa)"
        ),
    ),
    (NO_RESPIRA, re.compile(r"no (?:esta |puede )?respir|sin respirar|dej[oa] de respirar")),
    (
        SANGRADO,
        re.compile(
            r"sangr\w* (?:mucho|demasiado|harto|bastante)|mucha sangre|hemorragi|desangr|"
            r"no (?:para|deja) de sangrar"
        ),
    ),
    (
        INCONSCIENTE,
        re.compile(r"inconscien|desmayad|no (?:responde|reacciona)|perdio el conocimiento"),
    ),
    (INCENDIO, re.compile(r"incendi|en llamas|se (?:esta )?quemando|\bfuego\b|prendio fuego")),
)


ANIMAL = re.compile(
    r"\b(?:gat[oa]s?|gatic[oa]s?|perr[oa]s?|perrit[oa]s?|mascotas?|animal(?:es|ito)?|"
    r"caballos?|vacas?|gallinas?|pajar(?:o|os|ito)|loros?)\b"
)
PERSON = re.compile(
    r"\b(?:personas?|gente|senor(?:a|es|as)?|nin[oa]s?|bebes?|hombres?|mujer(?:es)?|"
    r"abuel[oa]s?|vecin[oa]s?|familia|hij[oa]s?|mama|papa|alguien|muert[oa]s?|"
    r"herman[oa]s?|esposo|esposa|amig[oa]s?|tio|tia|primo|prima)\b"
)
ANIMAL_NOTE = "el texto habla solo de animales: atrapados, no respira y sangrado no fijan piso"
NOT_FOR_ANIMALS = frozenset({ATRAPADOS, NO_RESPIRA, SANGRADO, INCONSCIENTE})

ORDER = {
    name: i
    for i, name in enumerate(
        (SOS, ATRAPADOS, NO_RESPIRA_O_SANGRADO, NO_RESPIRA, SANGRADO, INCONSCIENTE, INCENDIO)
    )
}


@dataclass(frozen=True, slots=True)
class RulesResult:
    floor: Urgency
    rules: tuple[str, ...]  # en el orden de arriba, sin repetir
    note: str | None = None  # por qué no se aplicó una regla (ANIMAL_NOTE)


NO_RULES = RulesResult(Urgency.BAJA, ())


def fold(text: str) -> str:
    """Minúsculas sin tildes ni diéresis: "Atrapadós" → "atrapados". La ñ queda como n."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def about_animal_only(text: str | None) -> bool:
    """El texto habla de un animal y no de personas ("hay un gato herido")."""
    folded = fold(text or "")
    return bool(ANIMAL.search(folded)) and not PERSON.search(folded)


def animal_only_report(reporte: Reporte) -> bool:
    """Reporte de un animal: el texto no nombra personas y la cabecera no las cuenta. Un SOS
    de escombros siempre es de una persona."""
    return (
        reporte.msg_type != MessageType.SOS_ESCOMBROS
        and reporte.people == People.NO_SE
        and about_animal_only(reporte.text)
    )


def apply_rules(reporte: Reporte) -> RulesResult:
    """Piso de urgencia según la cabecera y el texto del reporte."""
    flags = Flag(reporte.flags)
    fired: list[str] = []
    if reporte.msg_type == MessageType.SOS_ESCOMBROS:
        fired.append(SOS)
    if Flag.ATRAPADOS in flags or reporte.incident == Incident.ATRAPADOS:
        fired.append(ATRAPADOS)
    if Flag.NO_RESPIRA_O_SANGRADO in flags:
        fired.append(NO_RESPIRA_O_SANGRADO)
    if Flag.INCENDIO in flags or reporte.incident == Incident.INCENDIO:
        fired.append(INCENDIO)
    if reporte.text:
        folded = fold(reporte.text)
        fired += (name for name, pattern in TEXT_RULES if pattern.search(folded))
    note = None
    if animal_only_report(reporte) and set(fired) & NOT_FOR_ANIMALS:
        fired = [name for name in fired if name not in NOT_FOR_ANIMALS]
        note = ANIMAL_NOTE
    if not fired:
        return RulesResult(Urgency.BAJA, (), note) if note else NO_RULES
    return RulesResult(Urgency.CRITICA, tuple(sorted(set(fired), key=ORDER.__getitem__)), note)
