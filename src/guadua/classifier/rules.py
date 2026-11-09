"""Capa 1 del triaje: reglas de prioridad mínima (docs/diseno.md §11).

Las señales vitales fijan el piso en crítica: atrapados, no respira, sangrado fuerte,
inconsciente, incendio y SOS de escombros. Salen de la cabecera (banderas, tipo de mensaje
y de incidente) y del texto. Objetivo: menos de 1 ms por reporte.

Las reglas prefieren equivocarse hacia arriba: no interpretan negaciones ("no hay
atrapados" también sube a crítica). Bajar un caso es decisión de una persona, no del piso.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from guadua.classifier.urgency import Urgency
from guadua.protocol.header import Flag, Incident, MessageType
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


NO_RULES = RulesResult(Urgency.BAJA, ())


def fold(text: str) -> str:
    """Minúsculas sin tildes ni diéresis: "Atrapadós" → "atrapados". La ñ queda como n."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


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
    if not fired:
        return NO_RULES
    return RulesResult(Urgency.CRITICA, tuple(sorted(set(fired), key=ORDER.__getitem__)))
