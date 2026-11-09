"""Evaluación del agente de triaje (N8) con los conjuntos `test` y `ambiguo` (diseño §15).

    python -m guadua.agents.triaje_eval                    # sin LLM: solo cuántos le llegan
    LLM_PROVIDER=ollama OLLAMA_MODEL=qwen2.5:3b \\
        python -m guadua.agents.triaje_eval --todos --limite 40

Sin LLM mide lo que N8 recibe de N7: el porcentaje de reportes en la zona gris (objetivo
inicial ~15 %) y cuántos casos salen con nivel equivocado **sin** quedar marcados como
ambiguos (el agente nunca los ve: limitación del clasificador, que no entiende significado).

Con un LLM configurado (variables de `.env.example`, leídas del entorno) corre el agente
sobre los casos ambiguos y reporta: resueltos, bien resueltos, subidas/bajadas, pasos
promedio, herramientas inválidas y tiempo por caso. `--todos` le pasa todo el conjunto
`ambiguo` (no solo lo que N7 marcó), para medir qué tan bien razona el modelo.
"""

from __future__ import annotations

import argparse
import statistics
import time
from collections import Counter

from guadua.agents.triaje import AgenteTriaje, TriageCase
from guadua.classifier.evaluate import as_reporte
from guadua.classifier.model import CalibratedClassifier
from guadua.classifier.synthetic import Row, load
from guadua.classifier.triage import triage
from guadua.node.pipeline import normalize
from guadua.providers.fallback import llm_from_env
from guadua.providers.llm import LLMProvider
from guadua.store.event_types import UrgenciaEstimada
from guadua.store.events import Event, EventCategory

INVALID_MARKS = ("no permitida", "argumentos inválidos", "fuera del protocolo", "no devolvió")


def case_of(row: Row, i: int, urgency: UrgenciaEstimada) -> TriageCase:
    reporte = as_reporte(row, i)
    event = Event(
        origin_node="E",
        seq=i + 1,
        category=EventCategory.DERIVADO,
        type="urgencia_estimada",
        payload=urgency.model_dump(mode="json"),
        refs=(),
        received_at=0.0,
    )
    return TriageCase(event, urgency, normalize(reporte), ())


def coverage(rows: list[Row], classifier) -> dict[str, int]:
    """Lo que N7 le entrega a N8 en un conjunto."""
    counts = Counter()
    for i, row in enumerate(rows):
        u = triage(as_reporte(row, i), classifier)
        counts["n"] += 1
        counts["ambiguos"] += u.ambiguous
        counts["mal_sin_marcar"] += u.level != row.label and not u.ambiguous
    return dict(counts)


def run_agent(cases: list[tuple[Row, TriageCase]], llm: LLMProvider) -> dict[str, float]:
    agent = AgenteTriaje(llm)
    counts, steps, times = Counter(), [], []
    for row, case in cases:
        start = time.perf_counter()
        out = agent.run(case)
        times.append(time.perf_counter() - start)
        steps.append(sum(s.startswith("paso ") for s in out.steps))
        last = out.steps[-1] if out.steps else ""
        counts["casos"] += 1
        counts["invalidas"] += any(m in last for m in INVALID_MARKS)
        if out.ambiguous:
            counts["a_revision"] += 1
            continue
        counts["resueltos"] += 1
        counts["bien"] += out.level == row.label
        counts["subidas"] += out.level > case.urgency.level
        counts["bajadas"] += out.level < case.urgency.level
        counts["n7_bien"] += case.urgency.level == row.label
    return {
        **counts,
        "pasos_promedio": statistics.fmean(steps) if steps else 0.0,
        "s_por_caso": statistics.fmean(times) if times else 0.0,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--todos", action="store_true", help="todo `ambiguo`, no solo la zona gris")
    parser.add_argument("--limite", type=int, default=None, help="máximo de casos para el LLM")
    args = parser.parse_args(argv)

    rows = load()
    classifier = CalibratedClassifier.train(rows)
    print("Lo que N7 entrega al agente (zona gris = ambiguos):")
    for split in ("test", "ambiguo"):
        c = coverage([r for r in rows if r.split == split], classifier)
        print(
            f"  {split:8} {c['n']:4} reportes · al agente {c['ambiguos']:4} "
            f"({c['ambiguos'] / c['n']:.0%}) · mal y sin marcar {c.get('mal_sin_marcar', 0)}"
        )

    llm = llm_from_env()
    if llm is None:
        print("\nSin LLM configurado (LLM_PROVIDER, GEMINI_*, OLLAMA_*): no se corre el agente.")
        return
    cases = []
    for i, row in enumerate(r for r in rows if r.split == "ambiguo"):
        u = triage(as_reporte(row, i), classifier)
        if args.todos:
            u = u.model_copy(update={"ambiguous": True})
        if u.ambiguous:
            cases.append((row, case_of(row, i, u)))
    cases = cases[: args.limite]
    print(f"\nAgente con {llm.name} sobre {len(cases)} casos de `ambiguo`:")
    r = run_agent(cases, llm)
    resolved = r.get("resueltos", 0)
    print(
        f"  resueltos {resolved} · a revisión {r.get('a_revision', 0)} · "
        f"herramientas o respuestas inválidas {r.get('invalidas', 0)}\n"
        f"  bien resueltos {r.get('bien', 0)}/{resolved} (N7 tenía {r.get('n7_bien', 0)}) · "
        f"subidas {r.get('subidas', 0)} · bajadas {r.get('bajadas', 0)}\n"
        f"  pasos promedio {r['pasos_promedio']:.1f} · {r['s_por_caso']:.1f} s por caso"
    )


if __name__ == "__main__":
    main()
