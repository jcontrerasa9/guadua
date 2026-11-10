"""Evaluación del triaje con el conjunto de prueba separado (docs/diseno.md §11 y §15).

    python -m guadua.classifier.evaluate

Entrena con `train` (como el nodo) y mide sobre `test`, que nunca se usa para entrenar ni
para elegir el umbral:
- Sensibilidad y precisión en críticos, del clasificador solo y del sistema (reglas +
  clasificador, que es lo que publica el nodo). La sensibilidad es la métrica principal.
- Matriz de confusión del sistema.
- Cuántos reportes van a revisión humana (ambiguos), y cuántos críticos no detectados
  quedaron al menos marcados para revisión.
- Tiempo por reporte de reglas y clasificador (objetivos: < 1 ms y < 2 ms).
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from guadua.classifier.model import CalibratedClassifier
from guadua.classifier.rules import apply_rules
from guadua.classifier.synthetic import CONDICIONES, Row, load
from guadua.classifier.triage import triage
from guadua.classifier.urgency import Urgency
from guadua.protocol.header import MessageType
from guadua.store.event_types import Reporte

LEVELS = (Urgency.CRITICA, Urgency.ALTA, Urgency.MEDIA, Urgency.BAJA)
# La última condición de cada nivel nunca se entrena (classifier/synthetic.py).
UNSEEN = tuple(" | " + conditions[-1][1] for conditions in CONDICIONES.values())


def as_reporte(row: Row, i: int = 0) -> Reporte:
    """Una fila como reporte sin banderas: el ciudadano solo eligió el tipo y escribió."""
    return Reporte(
        report_id=f"{i:08x}-1",
        msg_type=MessageType.REPORTE,
        incident=row.incident,
        people=4,
        flags=0,
        barrio=0,
        device_time=0,
        text=row.text,
        channel="wifi",
        raw_hex="",
    )


@dataclass(frozen=True, slots=True)
class CriticalScore:
    sensitivity: float
    precision: float
    missed: tuple[str, ...]  # críticos que no salieron como críticos


def critical_score(truth: Sequence[Urgency], predicted: Sequence[Urgency], texts) -> CriticalScore:
    pairs = list(zip(truth, predicted, texts, strict=True))
    tp = sum(t == p == Urgency.CRITICA for t, p, _ in pairs)
    positives = sum(t == Urgency.CRITICA for t, _, _ in pairs)
    flagged = sum(p == Urgency.CRITICA for _, p, _ in pairs)
    missed = tuple(x for t, p, x in pairs if t == Urgency.CRITICA and p != Urgency.CRITICA)
    return CriticalScore(
        tp / positives if positives else 1.0, tp / flagged if flagged else 1.0, missed
    )


def confusion(truth: Sequence[Urgency], predicted: Sequence[Urgency]) -> dict[Urgency, dict]:
    matrix = {t: dict.fromkeys(LEVELS, 0) for t in LEVELS}
    for t, p in zip(truth, predicted, strict=True):
        matrix[t][p] += 1
    return matrix


def per_call_ms(fn: Callable[[], object], repeat: int) -> tuple[float, float]:
    """(mediana, p95) en milisegundos."""
    times = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        times.append((time.perf_counter() - start) * 1000)
    times.sort()
    return statistics.median(times), times[int(len(times) * 0.95) - 1]


@dataclass(frozen=True, slots=True)
class Report:
    test_size: int
    threshold: float
    classifier: CriticalScore
    system: CriticalScore
    unseen: CriticalScore  # solo frases de personas que nunca se entrenaron
    matrix: dict[Urgency, dict]
    rules_ms: tuple[float, float]
    classifier_ms: tuple[float, float]
    ambiguous_rate: float  # reportes de prueba que van a revisión ("ambiguo, revisar")
    missed_flagged: int  # críticos que no salieron como críticos pero sí quedaron ambiguos


def evaluate(rows: Sequence[Row], classifier: CalibratedClassifier | None = None) -> Report:
    classifier = classifier or CalibratedClassifier.train(rows)
    test = [r for r in rows if r.split == "test"]
    reportes = [as_reporte(r, i) for i, r in enumerate(test)]
    truth = [r.label for r in test]
    texts = [r.text for r in test]
    by_model = [classifier.predict(r.incident, r.text).level for r in test]
    estimates = [triage(rep, classifier) for rep in reportes]
    by_system = [u.level for u in estimates]
    unseen = [i for i, r in enumerate(test) if r.template.endswith(UNSEEN)]
    sample = reportes[:: max(1, len(reportes) // 200)]
    rules_ms = per_call_ms(lambda: [apply_rules(r) for r in sample], 50)
    model_ms = per_call_ms(lambda: [classifier.predict(r.incident, r.text) for r in sample], 5)
    per_report = len(sample)
    return Report(
        test_size=len(test),
        threshold=classifier.threshold,
        classifier=critical_score(truth, by_model, texts),
        system=critical_score(truth, by_system, texts),
        unseen=critical_score(
            [truth[i] for i in unseen], [by_system[i] for i in unseen], [texts[i] for i in unseen]
        ),
        matrix=confusion(truth, by_system),
        rules_ms=(rules_ms[0] / per_report, rules_ms[1] / per_report),
        classifier_ms=(model_ms[0] / per_report, model_ms[1] / per_report),
        ambiguous_rate=sum(u.ambiguous for u in estimates) / len(estimates),
        missed_flagged=sum(
            t == Urgency.CRITICA and u.level != Urgency.CRITICA and u.ambiguous
            for t, u in zip(truth, estimates, strict=True)
        ),
    )


def _pct(score: CriticalScore) -> str:
    return f"{score.sensitivity:10.1%}  {score.precision:9.1%}"


def _ms(times: tuple[float, float]) -> str:
    return f"{times[0]:6.3f} ms {times[1]:6.3f} ms"


def render(report: Report) -> str:
    name = {lvl: lvl.name.lower() for lvl in LEVELS}
    lines = [
        f"Conjunto de prueba: {report.test_size} reportes (combinaciones y frases no entrenadas)",
        f"Umbral de crítica: {report.threshold:.2f}",
        "",
        "Críticos            sensibilidad  precisión",
        f"  clasificador solo   {_pct(report.classifier)}",
        f"  reglas + clasif.    {_pct(report.system)}",
        f"    solo frases nuevas{_pct(report.unseen)}",
        "",
        f"A revisión humana (ambiguo): {report.ambiguous_rate:.1%} de los reportes de prueba",
        f"Críticos no detectados que igual quedaron ambiguos: {report.missed_flagged}"
        f" de {len(report.system.missed)}",
        "",
        "Matriz de confusión (reglas + clasificador; filas = real, columnas = publicado)",
        "           " + "".join(f"{name[p]:>9}" for p in LEVELS),
    ]
    for t in LEVELS:
        lines.append(f"  {name[t]:8} " + "".join(f"{report.matrix[t][p]:9}" for p in LEVELS))
    lines += [
        "",
        "Tiempo por reporte     mediana      p95   objetivo",
        f"  reglas              {_ms(report.rules_ms)}   < 1 ms",
        f"  clasificador        {_ms(report.classifier_ms)}   < 2 ms",
    ]
    if report.system.missed:
        lines += ["", "Críticos que no salieron como críticos:"]
        lines += [f"  - {text}" for text in report.system.missed[:10]]
    lines += ["", "Limitación: datos sintéticos (data/sinteticos/reportes.csv)."]
    return "\n".join(lines)


if __name__ == "__main__":
    print(render(evaluate(load())))
