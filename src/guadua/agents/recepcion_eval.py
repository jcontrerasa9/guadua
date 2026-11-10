"""Evaluación del agente de recepción (D5a): ¿la pregunta tiene que ver con el reporte?

    python -m guadua.agents.recepcion_eval            # solo reglas
    python -m guadua.agents.recepcion_eval --llm      # con la cadena de .env.example
    docker exec guadua-node-a-1 python -m guadua.agents.recepcion_eval --llm

Casos escritos como los manda la PWA (personas "no sé", observación directa), varios
sacados de pruebas reales. Cada caso dice qué datos son pertinentes de preguntar (`ok`, con
`None` = no preguntar) y qué debe elegir la parte de reglas (`rules`). Con LLM, además del
dato elegido, se imprime el texto de la pregunta para revisarlo a ojo: si usa las palabras
del reporte y si les sirve a los rescatistas.
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from dataclasses import dataclass

from guadua.agents.recepcion import AgenteRecepcion, QuestionCase, rule_candidates, why_not_ask
from guadua.node.pipeline import normalize
from guadua.protocol.header import Flag, Incident, MessageType, People
from guadua.providers.fallback import llm_from_env
from guadua.providers.llm import LLMProvider
from guadua.store.event_types import Reporte
from guadua.store.events import Event, EventCategory

VITAL, TRAP, GAS, PEOPLE, FREE = (
    "no_respira_o_sangrado", "atrapados", "gas_o_cables", "personas", "libre",
)  # fmt: skip
DIRECT = Flag.OBSERVACION_DIRECTA


@dataclass(frozen=True, slots=True)
class Case:
    text: str
    incident: Incident
    rules: str | None  # lo que deben elegir las reglas
    ok: frozenset[str | None]  # lo pertinente de preguntar (None: no preguntar)
    flags: Flag = DIRECT
    people: People = People.NO_SE
    msg_type: MessageType = MessageType.REPORTE


def _ok(*keys: str | None) -> frozenset[str | None]:
    return frozenset(keys)


CASES: tuple[Case, ...] = (
    Case("Creo que hay un gato herido", Incident.HERIDO, None, _ok(None, FREE)),
    Case("Hay un perro atrapado bajo los escombros", Incident.COLAPSO, GAS, _ok(GAS, FREE, None)),
    Case("Tiene un brazo doblado raro", Incident.HERIDO, VITAL, _ok(VITAL)),
    Case("Mi abuela se cayó y no se puede levantar", Incident.HERIDO, VITAL, _ok(VITAL)),
    Case("Hay un herido y huele a gas", Incident.HERIDO, VITAL, _ok(VITAL)),
    Case("Accidente de moto, el señor sangra mucho", Incident.HERIDO, PEOPLE, _ok(PEOPLE, FREE)),
    Case("Hay un muerto", Incident.INCENDIO, TRAP, _ok(TRAP, GAS, PEOPLE, FREE), Flag.INCENDIO),
    Case("Se está incendiando una cobija", Incident.INCENDIO, TRAP, _ok(TRAP, FREE)),
    Case("Se quema una casa y hay mucho humo", Incident.INCENDIO, TRAP, _ok(TRAP, GAS, FREE)),
    Case("Hay humo saliendo de la cocina del vecino", Incident.INCENDIO, TRAP,
         _ok(TRAP, GAS, FREE)),
    Case("Se cayó una casa en Chipre", Incident.COLAPSO, TRAP, _ok(TRAP, FREE)),
    Case("El edificio está agrietado y la gente sigue adentro", Incident.COLAPSO, TRAP,
         _ok(TRAP, GAS, FREE)),
    Case("colapso casa 3 atrapados", Incident.COLAPSO, VITAL, _ok(VITAL), Flag.ATRAPADOS,
         People.DE_2_A_5),
    Case("Hay dos niños atrapados en el segundo piso", Incident.ATRAPADOS, VITAL, _ok(VITAL)),
    Case("Puede ser el gas o la basura", Incident.FUGA_GAS, None, _ok(None, FREE),
         DIRECT | Flag.RIESGO_ESCENA),
    Case("Huele mucho a gas en el edificio", Incident.FUGA_GAS, None, _ok(None, FREE)),
    Case("Se cayó un árbol y tapó la vía", Incident.VIA_BLOQUEADA, None, _ok(None, FREE)),
    Case("Cables chispeando en la calle", Incident.OTRO, None, _ok(None, FREE)),
    Case("", Incident.ATRAPADOS, None, _ok(None), msg_type=MessageType.SOS_ESCOMBROS),
    Case("Se cayó el techo", Incident.COLAPSO, None, _ok(None), DIRECT | Flag.BATERIA_CRITICA),
)  # fmt: skip


def reporte_of(case: Case, i: int) -> Reporte:
    return Reporte(
        report_id=f"eeeeeeee-{i + 1}",
        msg_type=case.msg_type,
        incident=case.incident,
        people=case.people,
        flags=int(case.flags),
        barrio=1,
        device_time=0,
        text=case.text or None,
        channel="wifi",
        raw_hex="00",
    )


def question_case(reporte: Reporte, kinds) -> QuestionCase:
    report = normalize(reporte)
    event = Event(
        origin_node="E",
        seq=1,
        category=EventCategory.DERIVADO,
        type="reporte_normalizado",
        payload=report.model_dump(mode="json"),
        refs=(("E", 1),),
        received_at=0.0,
    )
    return QuestionCase(event, reporte, report, rule_candidates(reporte, report), kinds)


@dataclass(frozen=True, slots=True)
class Result:
    case: Case
    key: str | None
    text: str | None
    source: str | None
    seconds: float

    @property
    def pertinent(self) -> bool:
        return self.key in self.case.ok


def evaluate(llm: LLMProvider | None = None) -> list[Result]:
    agent = AgenteRecepcion(llm)
    results = []
    for i, case in enumerate(CASES):
        reporte = reporte_of(case, i)
        started = time.monotonic()
        q = None
        if why_not_ask(reporte) is None:
            q = agent.run(question_case(reporte, agent.kinds))
        took = time.monotonic() - started
        results.append(
            Result(case, q.key if q else None, q.text if q else None, q.source if q else None, took)
        )
    return results


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--llm", action="store_true", help="usar la cadena de LLM del entorno")
    args = parser.parse_args(argv)
    llm = llm_from_env() if args.llm else None
    if args.llm and llm is None:
        parser.error("no hay LLM configurado (LLM_PROVIDER, GEMINI_*, OLLAMA_*)")
    print(f"LLM: {llm.name if llm else 'ninguno (reglas)'} · {len(CASES)} casos\n")
    results = evaluate(llm)
    for r in results:
        mark = "✓" if r.pertinent else "✗"
        print(f"{mark} [{r.case.incident.name.lower()}] {r.case.text or '(sin texto)'}")
        source = f"{r.source or '—'}, {r.seconds:.1f} s"
        print(f"    → {r.key or 'no pregunta'} · {r.text or '—'} ({source})")
    counts = Counter(r.pertinent for r in results)
    sources = Counter(r.source for r in results if r.source)
    print(f"\nPertinentes: {counts[True]}/{len(results)} · por fuente: {dict(sources)}")


if __name__ == "__main__":
    main()
