"""Capa 2 del triaje: clasificador local calibrado (docs/diseno.md §11). Extra `ml`.

TF-IDF de n-gramas de caracteres (aguanta faltas de ortografía: "atrapaos", "inconsiente")
más regresión logística calibrada con `CalibratedClassifierCV`. Entra el tipo de incidente
que marcó el ciudadano y el texto; salen las probabilidades de los 4 niveles.

**Umbral de crítica.** Se elige al entrenar, con validación cruzada agrupada por par
(situación, condición) dentro de `train`, de modo que cada pliegue evalúa combinaciones que
no vio: es el más alto que deja la sensibilidad en críticos ≥ `TARGET_SENSITIVITY`. Si la
probabilidad de crítica lo alcanza, el nivel es crítica aunque otra clase tenga más
probabilidad; si no, gana la clase más probable. Así el umbral favorece no dejar pasar
críticos (§11).

Se entrena al arrancar el nodo a partir del CSV versionado (no se guarda ningún archivo de
modelo): con la misma semilla, todos los nodos quedan con el mismo modelo.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold, StratifiedKFold
from threadpoolctl import threadpool_limits

from guadua.classifier.rules import fold
from guadua.classifier.synthetic import Row
from guadua.classifier.triage import PROBABILITY_KEYS, Prediction
from guadua.classifier.urgency import Urgency
from guadua.protocol.header import Incident

SEED = 7
TARGET_SENSITIVITY = 0.99  # alto: esta validación ve frases conocidas en pares nuevos
THRESHOLDS = tuple(round(t, 2) for t in np.arange(0.5, 0.04, -0.05))
UNFAMILIAR_RATIO = 0.25  # desde 1 de cada 4 palabras desconocidas, el caso es ambiguo
WORD = re.compile(r"[a-z]{3,}")  # sobre texto sin tildes; las palabras cortas no cuentan


def features(incident: Incident, text: str | None) -> str:
    """Lo que ve el clasificador: el tipo de incidente como palabra, más el texto."""
    return f"tipo_{incident.name.lower()} {fold(text or '')}".strip()


def _fit(
    texts: Sequence[str], labels: Sequence[int]
) -> tuple[TfidfVectorizer, CalibratedClassifierCV]:
    vectorizer = TfidfVectorizer(
        analyzer="char_wb", ngram_range=(2, 4), sublinear_tf=True, min_df=2, lowercase=False
    )
    matrix = vectorizer.fit_transform(texts)
    model = CalibratedClassifierCV(
        LogisticRegression(C=4.0, solver="newton-cg"),  # el más rápido con estos datos
        method="sigmoid",
        cv=StratifiedKFold(n_splits=5, shuffle=True, random_state=SEED),
        ensemble=False,  # un solo modelo al predecir: más rápido
    )
    model.fit(matrix, labels)
    return vectorizer, model


def choose_threshold(p_critical: np.ndarray, is_critical: np.ndarray) -> float:
    """El umbral más alto con sensibilidad en críticos ≥ TARGET_SENSITIVITY."""
    for threshold in THRESHOLDS:
        if (p_critical[is_critical] >= threshold).mean() >= TARGET_SENSITIVITY:
            return threshold
    return THRESHOLDS[-1]


def _deletions(word: str) -> set[str]:
    return {word[:i] + word[i + 1 :] for i in range(len(word))}


class Vocabulary:
    """Palabras que el clasificador vio al entrenar, para saber cuándo un texto le es ajeno.

    El clasificador no entiende significado: ante palabras que nunca vio ("tortuga",
    "explosión") responde con la categoría y el resto del texto, a veces con mucha confianza.
    Por eso un texto con muchas palabras desconocidas se marca ambiguo y lo revisa alguien.

    Una palabra es conocida si está a una letra de distancia de una vista (borrar, agregar o
    cambiar una letra): las faltas de ortografía comunes ("persnas") no cuentan como
    desconocidas. Se compara por borrados, sin recorrer el vocabulario.
    """

    def __init__(self, texts: Iterable[str]) -> None:
        words = {w for text in texts for w in WORD.findall(fold(text))}
        self._index = words | {d for w in words for d in _deletions(w)}
        self._words = words

    def __len__(self) -> int:
        return len(self._words)

    def knows(self, word: str) -> bool:
        # Exacta o falta una letra · sobra una letra o cambió una (borrado común).
        return word in self._index or any(d in self._index for d in _deletions(word))

    def unknown(self, text: str | None) -> list[str]:
        return [w for w in WORD.findall(fold(text or "")) if not self.knows(w)]

    def unknown_ratio(self, text: str | None) -> float:
        words = WORD.findall(fold(text or ""))
        return len(self.unknown(text)) / len(words) if words else 0.0


class CalibratedClassifier:
    """Implementa `UrgencyClassifier` (classifier/triage.py)."""

    def __init__(
        self,
        vectorizer: TfidfVectorizer,
        model: CalibratedClassifierCV,
        threshold: float,
        vocabulary: Vocabulary,
    ) -> None:
        self.vectorizer = vectorizer
        self.model = model
        self.threshold = threshold
        self.vocabulary = vocabulary
        self._levels = tuple(Urgency(int(c)) for c in model.classes_)
        self._critical = self._levels.index(Urgency.CRITICA)

    @classmethod
    def train(cls, rows: Sequence[Row]) -> CalibratedClassifier:
        """Entrena con las filas `train` y elige el umbral sin mirar `test`.

        Con un solo hilo: con estos datos es igual de rápido (~2 s) y evita que tres nodos
        arrancando a la vez en la misma máquina se peleen los núcleos (pasaba de 2 s a 34 s).
        """
        with threadpool_limits(limits=1):
            return cls._train(rows)

    @classmethod
    def _train(cls, rows: Sequence[Row]) -> CalibratedClassifier:
        train = [r for r in rows if r.split == "train"]
        texts = [features(r.incident, r.text) for r in train]
        labels = np.array([int(r.label) for r in train])
        groups = [r.template for r in train]  # como en test: combinaciones nuevas
        p_critical = np.zeros(len(train))
        for fit_idx, eval_idx in GroupKFold(n_splits=4).split(texts, labels, groups):
            vectorizer, model = _fit([texts[i] for i in fit_idx], labels[fit_idx])
            probs = model.predict_proba(vectorizer.transform([texts[i] for i in eval_idx]))
            p_critical[eval_idx] = probs[:, list(model.classes_).index(int(Urgency.CRITICA))]
        threshold = choose_threshold(p_critical, labels == int(Urgency.CRITICA))
        vocabulary = Vocabulary(r.text for r in train)
        return cls(*_fit(texts, labels), threshold, vocabulary)

    def predict(self, incident: Incident, text: str | None) -> Prediction:
        probs = self.model.predict_proba(self.vectorizer.transform([features(incident, text)]))[0]
        if probs[self._critical] >= self.threshold:
            level = Urgency.CRITICA
        else:
            level = self._levels[int(np.argmax(probs))]
        return Prediction(
            level=level,
            probabilities={
                PROBABILITY_KEYS[lvl]: round(float(p), 4)
                for lvl, p in sorted(zip(self._levels, probs, strict=True), reverse=True)
            },
            unfamiliar=self.vocabulary.unknown_ratio(text) >= UNFAMILIAR_RATIO,
        )
