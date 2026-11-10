"""Lectura del texto de una transmisión de radio con reglas fijas (D7). Sin LLM.

Lo usan el agente de recepción al separar incidentes (agents/recepcion_radio.py) y la
normalización (node/pipeline.py). Todo se compara sin tildes y en minúsculas, conservando
las posiciones del texto original para poder cortarlo.

- `direct_cue`: ¿lo vio quien habla ("estamos en el sitio") o se lo contaron ("nos
  comentan que")? Vale la pista más cercana **antes** del suceso; si no hay, la primera
  después. Sin pista: None (desconocido; nunca se supone directo).
- `incidents`: tipos de incidente mencionados, en orden de aparición.
- `people`: personas dichas con número ("dos atrapados", "un herido"); si no, "no sé".
- `barrios`: barrios del listado offline (data/barrios.json) que se nombran.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from guadua.classifier import risk
from guadua.classifier import rules as R
from guadua.protocol.header import Incident, People

BARRIOS_PATH = Path(__file__).resolve().parents[3] / "data" / "barrios.json"


def fold_keep(text: str) -> str:
    """Como `rules.fold` pero con el mismo largo que el texto: las posiciones coinciden."""
    return "".join((R.fold(c) or c)[0] for c in text)


# --- Directo o de segunda mano -----------------------------------------------------------

DIRECT = re.compile(
    r"\b(?:estamos (?:en|aqui|aca|llegando|atendiendo|viendo|frente)|en el sitio|en sitio|"
    r"en la escena|en el lugar|llegamos|acabamos de llegar|vemos|estamos observando|observamos|"
    r"confirmo|confirmamos|confirmado en sitio|tenemos a la vista|tengo a la vista|"
    r"tenemos (?:un|una|dos|tres|\d)|aqui (?:en|tenemos|hay)|desde aqui)\b"
)
SECOND_HAND = re.compile(
    r"\b(?:nos (?:comentan|informan|reportan|dicen|cuentan|avisan|acaban de informar)|"
    r"me (?:comentan|dijeron|contaron|informan|dicen)|"
    r"(?:comentan|informan|reportan|dicen|cuentan|avisan) que|al parecer|"
    r"segun (?:la comunidad|los vecinos|un vecino|una vecina|reportes?|informacion)|"
    r"la comunidad (?:dice|informa|reporta)|parece (?:ser )?que|se dice que|supuestamente|"
    r"sin confirmar|por confirmar|llamada de|reporte ciudadano|nos llego (?:el )?reporte)\b"
)


def direct_cue(text: str, anchor: int | None = None) -> bool | None:
    """True directo, False segunda mano, None sin pista. `anchor`: posición del suceso
    (por defecto, el primero que se mencione)."""
    folded = fold_keep(text)
    cues = sorted(
        [(m.start(), False) for m in SECOND_HAND.finditer(folded)]
        + [(m.start(), True) for m in DIRECT.finditer(folded)]
    )
    if not cues:
        return None
    if anchor is None:
        found = incidents(text)
        anchor = found[0][0] if found else len(text)
    before = [c for c in cues if c[0] <= anchor]
    return before[-1][1] if before else cues[0][1]


def any_direct_cue(text: str) -> bool:
    return bool(DIRECT.search(fold_keep(text)))


# --- Tipo de incidente -------------------------------------------------------------------

INCIDENT_PATTERNS: tuple[tuple[Incident, re.Pattern[str]], ...] = (
    (Incident.ATRAPADOS, dict(R.TEXT_RULES)[R.ATRAPADOS]),
    (Incident.INCENDIO, re.compile(dict(R.TEXT_RULES)[R.INCENDIO].pattern + r"|conato|\bhumo\b")),
    (
        Incident.COLAPSO,
        re.compile(
            r"colaps|derrumb|desplom|se (?:cayo|vino abajo|vinieron abajo) (?:una|la|el|un)|"
            r"deslizamiento|avalancha|\balud\b|vivienda caida|casa caida|placa caida"
        ),
    ),
    (Incident.FUGA_GAS, re.compile(r"fuga de gas|escape de gas|olor a gas|huele a gas|\bfuga\b")),
    (Incident.HERIDO, re.compile(r"herid|lesionad|fractur|golpead|lastimad|quemaduras?\b")),
    (Incident.VIA_BLOQUEADA, dict(risk.TEXT_PATTERNS)[risk.VIA_BLOQUEADA]),
)


def incidents(text: str) -> list[tuple[int, Incident]]:
    """(posición, tipo) de cada tipo mencionado, por su primera aparición."""
    folded = fold_keep(text)
    found = []
    for incident, pattern in INCIDENT_PATTERNS:
        if m := pattern.search(folded):
            found.append((m.start(), incident))
    return sorted(found)


def main_incident(text: str) -> Incident:
    found = incidents(text)
    return found[0][1] if found else Incident.OTRO


# --- Personas ----------------------------------------------------------------------------

NUMBERS = {
    "un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
    "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "quince": 15,
    "veinte": 20, "treinta": 30, "cuarenta": 40, "cincuenta": 50, "cien": 100,
}  # fmt: skip
PEOPLE_WORDS = (
    r"personas?|heridos?|heridas?|atrapad[oa]s?|lesionad[oa]s?|victimas?|ninos?|ninas?|"
    r"adult[oa]s?|menores|ancian[oa]s?|pacientes?|muert[oa]s?|fallecid[oa]s?|"
    r"senor(?:a|es|as)?|hombres?|mujer(?:es)?|ocupantes?|habitantes?"
)
PEOPLE = re.compile(rf"\b(\d{{1,3}}|{'|'.join(NUMBERS)})\s+(?:[a-z]+\s+)?(?:{PEOPLE_WORDS})\b")


def people_count(text: str) -> People:
    counts = [
        int(m.group(1)) if m.group(1).isdigit() else NUMBERS[m.group(1)]
        for m in PEOPLE.finditer(fold_keep(text).lower())
    ]
    if not counts or max(counts) < 1:
        return People.NO_SE
    n = max(counts)
    if n == 1:
        return People.UNA
    if n <= 5:
        return People.DE_2_A_5
    return People.DE_6_A_20 if n <= 20 else People.MAS_DE_20


# --- Barrios -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Barrio:
    code: int
    name: str
    pattern: re.Pattern[str]


@cache
def barrios() -> tuple[Barrio, ...]:
    try:
        data = json.loads(BARRIOS_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return ()
    out = []
    for b in data:
        name = R.fold(b["name"])
        variants = {name}
        if name.startswith("av. "):
            rest = name[4:]
            variants |= {f"avenida {rest}", f"av {rest}"}
        alternatives = "|".join(re.escape(v).replace(r"\ ", r"\s+") for v in sorted(variants))
        out.append(Barrio(b["code"], b["name"], re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")))
    return tuple(out)


def barrios_in(text: str) -> list[tuple[int, Barrio]]:
    """(posición, barrio) de cada barrio nombrado, por su primera aparición."""
    folded = fold_keep(text).lower()
    found = [(m.start(), b) for b in barrios() if (m := b.pattern.search(folded))]
    return sorted(found, key=lambda item: item[0])


def first_barrio(text: str) -> Barrio | None:
    found = barrios_in(text)
    return found[0][1] if found else None


def barrio_by_name(name: str) -> Barrio | None:
    folded = R.fold(name).strip()
    return next((b for b in barrios() if b.pattern.fullmatch(folded)), None)


# --- Lo que no se puede perder al separar --------------------------------------------------


def signals(text: str) -> set[str]:
    """Señales vitales y de riesgo, y barrios: lo que cada parte debe conservar."""
    folded = R.fold(text)
    found = {name for name, pattern in R.TEXT_RULES if pattern.search(folded)}
    found |= {name for name, pattern in risk.TEXT_PATTERNS if pattern.search(folded)}
    found |= {f"barrio {b.name}" for _, b in barrios_in(text)}
    return found
