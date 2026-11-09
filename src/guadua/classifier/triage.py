"""Urgencia sugerida de un reporte: reglas (capa 1) + clasificador (capa 2). docs/diseno.md §11.

    piso  = piso de las reglas                                (crítica si hay señal vital)
    nivel = max(piso, nivel del clasificador)                 (apply_floor)

El **piso** es duro: nadie automático lo baja, tampoco el agente de triaje (regla 1 de
AGENTS.md). Lo que el clasificador agregó por encima del piso es una sugerencia: el agente de
triaje (N8, con LLM) podrá bajarla, un nivel por vez, con motivo y nunca por debajo del piso.
La decisión final siempre es de una persona.

- `source` dice qué capa fijó el nivel: "reglas" si se activó alguna (o si no hay
  clasificador), "clasificador" si el nivel salió de él.
- **Zona gris** → `ambiguous=True` ("ambiguo, revisar"): ninguna regla se activó y el
  clasificador duda (probabilidad máxima < 0,5 o las dos primeras a menos de 0,15), o no
  hay clasificador, o el texto tiene muchas palabras que el clasificador nunca vio (no
  entiende significado: ver `Vocabulary` en model.py). Mientras N8 no exista, ese caso lo
  revisa una persona.
- Si el clasificador falla o no está instalado (extra `ml`), quedan las reglas.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from typing import Protocol

from guadua.classifier.rules import apply_rules
from guadua.classifier.urgency import Urgency, apply_floor
from guadua.protocol.header import Incident
from guadua.store.event_types import Reporte, UrgenciaEstimada

logger = logging.getLogger("guadua.triaje")

PROBABILITY_KEYS = {
    Urgency.CRITICA: "critica",
    Urgency.ALTA: "alta",
    Urgency.MEDIA: "media",
    Urgency.BAJA: "baja",
}
GRAY_MAX_PROBABILITY = 0.5  # umbral inicial del diseño; se ajusta con el conjunto de prueba
GRAY_MARGIN = 0.15  # "las dos categorías principales muy parejas"


@dataclass(frozen=True, slots=True)
class Prediction:
    level: Urgency
    probabilities: dict[str, float] = field(default_factory=dict)  # claves de PROBABILITY_KEYS
    unfamiliar: bool = False  # el texto tiene muchas palabras que el clasificador nunca vio


class UrgencyClassifier(Protocol):
    """Capa 2. La implementación con scikit-learn está en classifier/model.py."""

    def predict(self, incident: Incident, text: str | None) -> Prediction: ...


def in_gray_zone(probabilities: Mapping[str, float]) -> bool:
    top = sorted(probabilities.values(), reverse=True)
    if not top:
        return True
    second = top[1] if len(top) > 1 else 0.0
    return top[0] < GRAY_MAX_PROBABILITY or top[0] - second < GRAY_MARGIN


def triage(
    reporte: Reporte,
    classifier: UrgencyClassifier | None,
    risk_signals: tuple[str, ...] = (),
) -> UrgenciaEstimada:
    rules = apply_rules(reporte)
    prediction = None
    if classifier is not None:
        try:
            prediction = classifier.predict(reporte.incident, reporte.text)
        except Exception:  # si falla el clasificador, quedan las reglas
            logger.exception("falló el clasificador con el reporte %s", reporte.report_id)
    level, source = rules.floor, "reglas"
    if prediction is not None:
        level = apply_floor(level, prediction.level)
        if not rules.rules:
            source = "clasificador"
    ambiguous = not rules.rules and (
        prediction is None or prediction.unfamiliar or in_gray_zone(prediction.probabilities)
    )
    return UrgenciaEstimada(
        report_id=reporte.report_id,
        level=level,
        floor=rules.floor,
        source=source,
        probabilities=prediction.probabilities if prediction else {},
        rules=rules.rules,
        risk_signals=risk_signals,
        ambiguous=ambiguous,
    )


@cache
def default_classifier() -> UrgencyClassifier | None:
    """El clasificador entrenado con data/sinteticos/reportes.csv, una vez por proceso.

    None si falta el extra `ml` o el CSV: el nodo sigue, solo con reglas.
    """
    try:
        from guadua.classifier.model import CalibratedClassifier
        from guadua.classifier.synthetic import load
    except ImportError:
        logger.warning("sin scikit-learn (extra ml): la urgencia sale solo de las reglas")
        return None
    try:
        rows = load()
    except OSError:
        logger.warning("sin datos sintéticos: la urgencia sale solo de las reglas")
        return None
    classifier = CalibratedClassifier.train(rows)
    logger.info("clasificador entrenado; umbral de crítica %.2f", classifier.threshold)
    return classifier
