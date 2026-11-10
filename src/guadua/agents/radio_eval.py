"""Evaluación de la radio VHF (D7): ¿separa bien los sucesos y entiende el audio?

    python -m guadua.agents.radio_eval                 # separación con reglas, sobre texto
    python -m guadua.agents.radio_eval --llm           # con la cadena de LLM del entorno
    python -m guadua.agents.radio_eval --clips         # + transcribe los clips grabados
    docker exec guadua-node-a-1 python -m guadua.agents.radio_eval --clips --llm

Casos:
- Los guiones de data/radio/clips.json (lo que el equipo lee al grabar).
- Casos difíciles escritos aparte, sin ajustar las reglas a ellos: dos sucesos en el mismo
  barrio, "directo" que viene del contexto, conectores raros. Las reglas fallan algunos a
  propósito; ahí se ve si el LLM ayuda.

Por caso se compara, en orden, cada parte: tipo, barrio y directo (true / false / null).
Con `--clips`, además, el error de palabras (WER) de Whisper frente al guion y el tiempo.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from guadua.agents.recepcion_radio import SeparadorRadio, Split, SplitCase
from guadua.classifier.rules import fold
from guadua.providers.fallback import llm_from_env
from guadua.providers.llm import LLMProvider
from guadua.providers.transcriber import ROOT, TranscriberUnavailable, transcriber_from_env
from guadua.radio.clips import EXTENSIONS
from guadua.store.event_types import TranscripcionRadio
from guadua.store.events import Event, EventCategory

CLIPS_PATH = ROOT / "data" / "radio" / "clips.json"

type Expected = tuple[tuple[str, str | None, bool | None], ...]  # (tipo, barrio, directo)


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    text: str
    parts: Expected
    audio: Path | None = None


HARD: tuple[Case, ...] = (
    Case("mismo_barrio_dos_casos",
         "Central, en el Centro tenemos dos casos: un incendio en la carrera 23 y un herido "
         "en la 25. Cambio.",
         (("incendio", "Centro", True), ("herido", "Centro", True))),
    Case("llegando_no_visto", "Nos reportan un incendio en Palermo, nosotros estamos llegando.",
         (("incendio", "Palermo", False),)),
    Case("colapso_con_vitales",
         "Estamos en Milán, el colapso dejó tres personas atrapadas y una con sangrado fuerte.",
         (("colapso", "Milán", True),)),
    Case("controlado_sin_pista", "Positivo, incendio controlado en Chipre, sin heridos.",
         (("incendio", "Chipre", None),)),
    Case("dos_rumores",
         "Central, nos dicen que hay un incendio en Palermo y otro incendio en Milán.",
         (("incendio", "Palermo", False), ("incendio", "Milán", False))),
    Case("unidad_reporta", "Unidad 2 reporta: árbol caído bloqueando la vía en El Cable.",
         (("via_bloqueada", "El Cable", None),)),
    Case("gas_y_cables", "Estamos en el sitio en Chipre, huele a gas y hay cables caídos.",
         (("fuga_gas", "Chipre", True),)),
    Case("dos_sin_pista",
         "Central, atención: colapso en la Avenida Santander con atrapados, y en Palermo un "
         "incendio pequeño ya controlado. Cambio.",
         (("colapso", "Av. Santander", None), ("incendio", "Palermo", None))),
    Case("falsa_alarma", "Llegamos a El Cable, no hay nada, falsa alarma. Cambio.",
         (("otro", "El Cable", True),)),
    Case("heridos_por_muro", "Nos informan dos heridos en el Centro por caída de un muro.",
         (("herido", "Centro", False),)),
    Case("directo_y_rumor_mismo_tipo",
         "Estamos en Palermo con el incendio. La comunidad dice que en Milán también hay una "
         "casa en llamas.",
         (("incendio", "Palermo", True), ("incendio", "Milán", False))),
    Case("repetido", "Hay humo en Chipre. Hay humo en Chipre, repito, humo en Chipre.",
         (("incendio", "Chipre", None),)),
)  # fmt: skip


def clip_cases() -> list[Case]:
    data = json.loads(CLIPS_PATH.read_text("utf-8"))
    out = []
    for c in data["clips"]:
        if not c["partes"]:
            continue
        audio = next(
            (p for ext in EXTENSIONS if (p := CLIPS_PATH.parent / f"{c['id']}{ext}").is_file()),
            None,
        )
        parts = tuple((p["tipo"], p["barrio"], p["directo"]) for p in c["partes"])
        out.append(Case(c["id"], c["guion"], parts, audio))
    return out


def words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", fold(text)).split()


def wer(reference: str, hypothesis: str) -> float:
    """Error de palabras: (sustituciones + borradas + insertadas) / palabras del guion."""
    ref, hyp = words(reference), words(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, start=1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1] / max(len(ref), 1)


def _case(text: str) -> SplitCase:
    payload = TranscripcionRadio(audio_id="0" * 16, text=text, direct=False)
    event = Event(origin_node="E", seq=1, category=EventCategory.HECHO,
                  type="transcripcion_radio", payload=payload.model_dump(mode="json"),
                  received_at=0.0)  # fmt: skip
    return SplitCase(event, payload)


def got(split: Split) -> Expected:
    return tuple(
        (p.incident.name.lower(), p.barrio.name if p.barrio else None, p.direct)
        for p in split.parts
    )


@dataclass(frozen=True, slots=True)
class Result:
    case: Case
    text: str  # lo que se separó: el guion o lo que transcribió Whisper
    got: Expected
    split_by: str
    seconds: float
    wer: float | None = None
    transcribe_s: float | None = None

    @property
    def parts_ok(self) -> bool:
        return len(self.got) == len(self.case.parts)

    @property
    def ok(self) -> bool:
        return self.got == self.case.parts


def evaluate(llm: LLMProvider | None, cases: list[Case], *, audio: bool) -> list[Result]:
    agent = SeparadorRadio(llm)
    transcriber = transcriber_from_env() if audio else None
    results = []
    for case in cases:
        text, error, took_t = case.text, None, None
        if transcriber is not None and case.audio is not None:
            started = time.monotonic()
            try:
                text = transcriber.transcribe(case.audio.read_bytes()).text
                error = wer(case.text, text)
            except TranscriberUnavailable as exc:
                print(f"  ✗ {case.id}: {exc}")
            took_t = time.monotonic() - started
        started = time.monotonic()
        split = agent.run(_case(text))
        took = time.monotonic() - started
        results.append(Result(case, text, got(split), split.split_by, took, error, took_t))
    return results


DIRECT_LABELS = {True: "directo", False: "segunda mano", None: "¿?"}


def _fmt(parts: Expected) -> str:
    return " | ".join(f"{t}, {b or 'sin lugar'}, {DIRECT_LABELS[d]}" for t, b, d in parts)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--llm", action="store_true", help="usar la cadena de LLM del entorno")
    parser.add_argument("--clips", action="store_true", help="transcribir los clips grabados")
    args = parser.parse_args(argv)
    llm = llm_from_env() if args.llm else None
    if args.llm and llm is None:
        parser.error("no hay LLM configurado (LLM_PROVIDER, GEMINI_*, OLLAMA_*)")
    cases = clip_cases() + list(HARD)
    with_audio = sum(1 for c in cases if c.audio)
    print(f"LLM: {llm.name if llm else 'ninguno (reglas)'} · {len(cases)} casos "
          f"({with_audio} con audio{'' if args.clips else ', sin transcribir'})\n")  # fmt: skip
    results = evaluate(llm, cases, audio=args.clips)
    for r in results:
        print(f"{'✓' if r.ok else '✗'} {r.case.id}  ({r.split_by}, {r.seconds:.1f} s)")
        if r.wer is not None:
            print(f"    Whisper: WER {r.wer:.0%} en {r.transcribe_s:.1f} s · «{r.text}»")
        if not r.ok:
            print(f"    esperado: {_fmt(r.case.parts)}\n    obtenido: {_fmt(r.got)}")
    n = len(results)
    hard = [r for r in results if r.case in HARD]
    print(f"\nSeparación exacta: {sum(r.ok for r in results)}/{n} "
          f"(difíciles {sum(r.ok for r in hard)}/{len(hard)}) · "
          f"número de partes correcto: {sum(r.parts_ok for r in results)}/{n}")  # fmt: skip
    wers = [r.wer for r in results if r.wer is not None]
    if wers:
        times = [r.transcribe_s for r in results if r.transcribe_s is not None]
        print(f"Whisper: WER medio {sum(wers) / len(wers):.0%} en {len(wers)} clips · "
              f"{sum(times) / len(times):.1f} s por clip")  # fmt: skip


if __name__ == "__main__":
    main()
