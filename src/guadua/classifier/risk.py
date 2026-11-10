"""Señales de riesgo para rescatistas (D3a). docs/diseno.md §11, "criterio, no regla".

Son peligros en la escena para quien llega a ayudar: gas, cables caídos, cilindros expuestos,
colapso secundario, vía bloqueada y fuego. **No cambian la urgencia**: la urgencia dice qué
tan grave está la víctima; el riesgo, qué unidad mandar y qué advertir. Los agentes de
triaje y coordinación las consideran, y la tarjeta las muestra en la franja de riesgo.

Salen de la cabecera (banderas y tipo de incidente) y del texto. Los nombres son los de
`data/riesgos_unidad.csv`, que usa coordinación para recomendar la unidad (D3b).

Como las reglas, prefieren equivocarse hacia arriba: no interpretan negaciones ("no huele
a gas" también marca gas). Una advertencia de más cuesta poco; una de menos puede costar un
rescatista.
"""

from __future__ import annotations

import re

from guadua.classifier.rules import INCENDIO, TEXT_RULES, fold
from guadua.protocol.header import Flag, Incident
from guadua.store.event_types import Reporte

GAS = "gas"
CABLES_CAIDOS = "cables_caidos"
GAS_O_CABLES = "gas_o_cables"  # bandera 3 de la cabecera: no dice cuál de los dos
CILINDROS_EXPUESTOS = "cilindros_expuestos"
COLAPSO_SECUNDARIO = "colapso_secundario"
VIA_BLOQUEADA = "via_bloqueada"
FUEGO = "incendio"

ORDER = (
    GAS,
    CABLES_CAIDOS,
    GAS_O_CABLES,
    CILINDROS_EXPUESTOS,
    COLAPSO_SECUNDARIO,
    VIA_BLOQUEADA,
    FUEGO,
)

# El texto se compara sin tildes y en minúsculas (ver `fold`).
TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (GAS, re.compile(r"\bgas\b|\bpropano\b")),
    (
        CABLES_CAIDOS,
        re.compile(
            r"\bcables?\b[^.]{0,25}?(?:caid|suelt|colgand|reventad|en el (?:piso|suelo)|chispe)|"
            r"\bpostes? (?:de (?:luz|energia) )?(?:caid|tumbad|partid)|se cayo (?:un |el )?poste|"
            r"chispa|electrocut|corto ?circuito"
        ),
    ),
    (CILINDROS_EXPUESTOS, re.compile(r"cilindr|pipeta|tanque de gas|bala de gas|pimpina")),
    (
        COLAPSO_SECUNDARIO,
        re.compile(
            r"(?:puede|va a|esta por|a punto de) (?:caer|colapsar|derrumbar|desplomar)se?|"
            r"grieta|agrietad|inestable|replica|riesgo de (?:colapso|derrumbe)|"
            r"se (?:sigue|siguen) (?:cayendo|derrumbando)"
        ),
    ),
    (
        VIA_BLOQUEADA,
        re.compile(
            r"\b(?:via|calle|carretera|camino)s? (?:esta |quedo )?"
            r"(?:bloquead|tapad|obstruid|cerrad|taponad)|"
            r"tap(?:o|aron) (?:la|el) (?:via|calle|carretera|camino)|"
            r"no hay paso|no (?:se puede|pueden?) pasar|arbol(?:es)? caid|"
            r"derrumbe (?:en|sobre) (?:la|el) (?:via|calle|carretera|camino)"
        ),
    ),
    (FUEGO, dict(TEXT_RULES)[INCENDIO]),  # la misma expresión que la regla de incendio
)

INCIDENT_SIGNALS = {
    Incident.FUGA_GAS: GAS,
    Incident.VIA_BLOQUEADA: VIA_BLOQUEADA,
    Incident.INCENDIO: FUEGO,
}


def detect_risk_signals(reporte: Reporte) -> tuple[str, ...]:
    """Señales de riesgo en la escena, en el orden de `ORDER` y sin repetir."""
    flags = Flag(reporte.flags)
    found: set[str] = set()
    if Flag.INCENDIO in flags:
        found.add(FUEGO)
    if signal := INCIDENT_SIGNALS.get(reporte.incident):
        found.add(signal)
    if reporte.text:
        folded = fold(reporte.text)
        found.update(name for name, pattern in TEXT_PATTERNS if pattern.search(folded))
    # La bandera solo dice "gas o cables": si ya se sabe cuál, sobra.
    if Flag.RIESGO_ESCENA in flags and not found & {GAS, CABLES_CAIDOS}:
        found.add(GAS_O_CABLES)
    return tuple(s for s in ORDER if s in found)
