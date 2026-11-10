"""Agente 2 · Triaje: ciclo con LLM en la zona gris (N8). docs/diseno.md §10 y §11.

Toma una `urgencia_estimada` de N7 marcada `ambiguous` (y que no sea suya) y la investiga
con un LLM en un ciclo de LangGraph:

    START → llm ─┬→ herramienta → llm …       (incidentes_cercanos, radio_en_zona,
                 │                             marcar_verificar_ya)
                 ├→ decidir → END              (fijar_urgencia o respuesta final)
                 └→ END                        (sin LLM, respuesta inválida, herramienta no
                                                permitida, 6 pasos o tiempo agotados)

Publica una `urgencia_estimada` **nueva** con `source="agente"` y `refs` a la de N7 (los
eventos no se editan). Los guardias (`decide_level`) aplican la regla 1 de AGENTS.md sobre
lo que proponga el LLM:

- subir: sin límite;
- bajar: solo un nivel desde el de N7, con una cita **textual** del reporte y nunca bajo el
  piso de las reglas (`apply_floor`);
- lo que no cumpla, y cualquier falla (sin LLM, `NullLLM`, JSON inválido, herramienta fuera
  de la lista blanca, pasos o tiempo agotados), deja el nivel de N7 y el caso en
  "ambiguo, revisar".

Cada paso queda en `steps` del evento (registro inmutable, se ve en "por qué está aquí") y
en el logger `guadua.triaje`. La decisión final siempre es de una persona.

`radio_en_zona` (D7) lee los reportes de la radio VHF del mismo lugar: evidencia, no
cambia el nivel por sí sola. Herramientas del diseño que todavía no existen (ver
docs/plan.md, "Pendiente de N8"): `respuesta_seguimiento` y `solicitar_verificacion_campo`
(necesita un tipo de evento nuevo). Mientras tanto, para pedir verificación está
`marcar_verificar_ya`.
"""

from __future__ import annotations

import json
import logging
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from guadua.classifier.cues import language_cues
from guadua.classifier.urgency import Urgency, apply_floor
from guadua.node.views import incident_details
from guadua.providers.llm import LLMProvider, LLMReply, LLMUnavailable, Message, ToolSpec
from guadua.store.event_types import ReporteNormalizado, UrgenciaEstimada
from guadua.store.events import Event, EventCategory, EventStore

logger = logging.getLogger("guadua.triaje")

MAX_STEPS = 6  # llamadas al LLM por caso (diseño §10)
TIME_BUDGET_S = 180.0  # tiempo por caso; cada llamada además tiene su propio timeout
MIN_QUOTE_CHARS = 3
MAX_STEP_CHARS = 300
NEARBY_LIMIT = 5
NEARBY_DEFAULT_MIN = 60
LEVEL_NAMES = {u: u.name.lower().replace("critica", "crítica") for u in Urgency}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "incidentes_cercanos",
        "Incidentes del mismo barrio o zona en los últimos minutos, con su nivel y reportes.",
        {
            "type": "object",
            "properties": {"minutos": {"type": "integer", "minimum": 10, "maximum": 180}},
        },
    ),
    ToolSpec(
        "radio_en_zona",
        "Lo que dijo la radio de Bomberos (VHF) en el mismo barrio o zona en los últimos "
        "minutos: texto, si lo vieron en el sitio y hace cuánto.",
        {
            "type": "object",
            "properties": {"minutos": {"type": "integer", "minimum": 10, "maximum": 180}},
        },
    ),
    ToolSpec(
        "marcar_verificar_ya",
        "Pide verificar ya el caso en campo (no cambia el nivel).",
        {"type": "object", "properties": {"motivo": {"type": "string"}}, "required": ["motivo"]},
    ),
    ToolSpec(
        "fijar_urgencia",
        "Termina con el nivel propuesto. Para bajar, cita textualmente el reporte en quote.",
        {
            "type": "object",
            "properties": {
                "level": {"type": "integer", "minimum": 1, "maximum": 4},
                "reason": {"type": "string"},
                "quote": {"type": "string"},
                "verify_now": {"type": "boolean"},
            },
            "required": ["level", "reason"],
        },
    ),
)
TOOL_NAMES = frozenset(t.name for t in TOOLS)

SYSTEM_PROMPT = f"""Eres el agente de triaje de un sistema de reportes de emergencia en \
Manizales. No diagnosticas. Revisas un reporte dudoso y propones su nivel de urgencia: \
1 baja, 2 media, 3 alta, 4 crítica.
Límites (se verifican con código; si no los cumples, el caso va a revisión humana):
- Puedes subir el nivel sin límite.
- Solo puedes bajar UN nivel respecto al nivel actual, nunca por debajo del piso, y debes \
copiar en "quote" las palabras exactas del reporte que lo justifican. Si dudas, no bajes.
- Tienes como máximo {MAX_STEPS} pasos. Usa herramientas solo si ayudan.
- Las "senales_de_riesgo" son peligros para los rescatistas (gas, cables caídos…), no \
gravedad de la víctima: menciónalas en tu motivo, pero NO suben ni bajan el nivel.
- La urgencia mide el riesgo para la vida de PERSONAS. Si "senales_de_lenguaje" dice \
"solo animales" y el texto no menciona personas afectadas, baja un nivel; en "quote" \
copia la frase del texto que nombra al animal (p. ej. "un perro"), no la señal.
- Si dice "duda en el texto", quien reporta no está seguro: no subas por eso y marca \
verify_now en true para que alguien lo verifique en campo.
- Una persona toma la decisión final.
Termina con fijar_urgencia o con {{"final": {{"level": n, "reason": "...", "quote": "...", \
"verify_now": false}}}}."""


# --- Guardias -----------------------------------------------------------------------------


def _norm(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return " ".join(plain.split())


def quote_in_text(quote: str, text: str | None) -> bool:
    """La cita existe en el texto del reporte (sin tildes, mayúsculas ni espacios extra)."""
    q = _norm(quote or "")
    return len(q) >= MIN_QUOTE_CHARS and q in _norm(text or "")


def decide_level(
    floor: Urgency, current: Urgency, proposed: int, quote: str | None, text: str | None
) -> tuple[Urgency | None, str]:
    """Regla 1. Devuelve (nivel aceptado, descripción) o (None, por qué se rechaza).

    `current` es el nivel de N7 (máximo entre piso y clasificador); `floor`, el de las reglas.
    """
    if isinstance(proposed, bool) or not isinstance(proposed, int) or proposed not in [*Urgency]:
        return None, f"nivel propuesto inválido: {proposed!r}"
    proposed, floor, current = Urgency(proposed), Urgency(floor), Urgency(current)
    name = LEVEL_NAMES
    if proposed > current:
        return apply_floor(floor, proposed), f"sube {name[current]} → {name[proposed]}"
    if proposed == current:
        return current, f"confirma {name[current]}"
    if proposed < floor:
        return None, f"propuso {name[proposed]}, bajo el piso de las reglas ({name[floor]})"
    if proposed < current - 1:
        return None, f"propuso bajar {name[current]} → {name[proposed]}: más de un nivel"
    if not quote_in_text(quote or "", text):
        return None, f"propuso bajar a {name[proposed]} sin una cita textual del reporte"
    return apply_floor(floor, proposed), f"bajada por el agente: {name[current]} → {name[proposed]}"


def needs_agent(urgency: UrgenciaEstimada) -> bool:
    """Solo la zona gris de N7, y nunca lo que ya publicó el agente (no hay bucle)."""
    return urgency.ambiguous and urgency.source != "agente"


# --- Caso y herramientas ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TriageCase:
    """Lo que el agente necesita, tomado en el hilo del nodo (el ciclo corre en otro)."""

    urgency_event: Event
    urgency: UrgenciaEstimada
    report: ReporteNormalizado
    events: tuple[Event, ...]  # copia del registro, para las herramientas

    @property
    def now(self) -> float:
        return self.urgency_event.received_at


class ToolError(ValueError):
    """Argumentos inválidos: el LLM pidió algo fuera de lo permitido."""


def _same_place(a: Any, b: Any) -> bool:
    if a.barrio is not None and b.barrio is not None:
        return a.barrio == b.barrio
    if a.geohash is not None and b.geohash is not None:
        return a.geohash[:6] == b.geohash[:6]  # ~1 km
    return False


def incidentes_cercanos(case: TriageCase, minutos: int = NEARBY_DEFAULT_MIN) -> list[dict]:
    """Otros incidentes del mismo lugar con actividad en la ventana, del más urgente al menos."""
    if isinstance(minutos, bool) or not isinstance(minutos, int) or not 10 <= minutos <= 180:
        raise ToolError(f"minutos debe ser un entero entre 10 y 180, llegó {minutos!r}")
    since = case.now - minutos * 60
    nearby = [
        d
        for d in incident_details(case.events)
        if case.report.report_id not in d.report_ids
        and d.updated_at >= since
        and _same_place(d.location, case.report.location)
    ]
    nearby.sort(key=lambda d: (-d.level, d.received_at))
    return [
        {
            "titulo": d.title,
            "nivel": LEVEL_NAMES[d.level],
            "reportes": d.report_count,
            "confianza": d.confidence,
            "rumor": d.rumor,
            "verificar_ya": d.verify_now,
            "hace_min": max(0, round((case.now - d.received_at) / 60)),
        }
        for d in nearby[:NEARBY_LIMIT]
    ]


def radio_en_zona(case: TriageCase, minutos: int = NEARBY_DEFAULT_MIN) -> list[dict]:
    """Reportes de radio (D7) del mismo lugar en la ventana, del más reciente al más antiguo.

    Solo los que dijeron un lugar: uno sin lugar (toda la ciudad) no dice nada de esta zona.
    Los de una transcripción que el operador corrigió no cuentan; cuentan los de la corrección.
    """
    if isinstance(minutos, bool) or not isinstance(minutos, int) or not 10 <= minutos <= 180:
        raise ToolError(f"minutos debe ser un entero entre 10 y 180, llegó {minutos!r}")
    if "location" in case.report.unknown_fields:
        return []
    since = case.now - minutos * 60
    corrected = {e.refs[0] for e in case.events if e.type == "transcripcion_radio" and e.refs}
    normalized = {
        e.payload["report_id"]: ReporteNormalizado.model_validate(e.payload)
        for e in case.events
        if e.type == "reporte_normalizado"
    }
    found = []
    for e in case.events:
        if e.type != "reporte" or e.payload.get("channel") != "vhf" or e.received_at < since:
            continue
        rid = e.payload["report_id"]
        n = normalized.get(rid)
        if (
            n is None
            or rid == case.report.report_id
            or (e.refs and e.refs[0] in corrected)
            or "location" in n.unknown_fields
            or not _same_place(n.location, case.report.location)
        ):
            continue
        found.append((e.received_at, n))
    found.sort(key=lambda item: -item[0])
    return [
        {
            "texto": n.text,
            "tipo": n.incident.name.lower(),
            "lo_vieron_en_el_sitio": n.direct_observation,
            "hace_min": max(0, round((case.now - at) / 60)),
        }
        for at, n in found[:NEARBY_LIMIT]
    ]


def _case_prompt(case: TriageCase) -> str:
    u, r = case.urgency, case.report
    place = r.location.label or (f"barrio {r.location.barrio}" if r.location.barrio is not None
                                 else f"geohash {r.location.geohash}")  # fmt: skip
    data = {
        "texto": r.text or "",
        "tipo": r.incident.name.lower(),
        "personas": r.people.name.lower(),
        "lugar": place,
        "observacion_directa": r.direct_observation,
        "senales_de_riesgo": list(u.risk_signals),
        "nivel_actual": LEVEL_NAMES[u.level],
        "piso_de_las_reglas": LEVEL_NAMES[u.floor],
        "probabilidades_del_clasificador": u.probabilities,
        "reglas_activadas": list(u.rules),
        "senales_de_lenguaje": list(language_cues(r.text)),  # classifier/cues.py
    }
    return "Reporte dudoso:\n" + json.dumps(data, ensure_ascii=False)


# --- Ciclo (LangGraph) --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Outcome:
    level: Urgency
    ambiguous: bool
    step: str


class _State(TypedDict):
    case: TriageCase
    messages: list[Message]
    steps: list[str]
    calls: int
    started: float
    verify_now: bool
    reply: LLMReply | None
    outcome: _Outcome | None


def _short(text: str) -> str:
    return text if len(text) <= MAX_STEP_CHARS else text[: MAX_STEP_CHARS - 1] + "…"


def _args(args: dict) -> str:
    return json.dumps(args, ensure_ascii=False, sort_keys=True)


class AgenteTriaje:
    def __init__(
        self,
        llm: LLMProvider,
        *,
        max_steps: int = MAX_STEPS,
        time_budget_s: float = TIME_BUDGET_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.llm = llm
        self.max_steps = max_steps
        self.time_budget_s = time_budget_s
        self.clock = clock
        self._graph = self._build_graph()

    # --- Entradas y salidas ---------------------------------------------------------------

    @staticmethod
    def prepare(urgency_event: Event, store: EventStore) -> TriageCase | None:
        """Arma el caso si la urgencia lo necesita. Se llama en el hilo del nodo."""
        urgency = UrgenciaEstimada.model_validate(urgency_event.payload)
        if not needs_agent(urgency) or not urgency_event.refs:
            return None
        normalized = store.get(urgency_event.refs[0])
        if normalized is None or normalized.type != "reporte_normalizado":
            return None
        report = ReporteNormalizado.model_validate(normalized.payload)
        return TriageCase(urgency_event, urgency, report, tuple(store.since({})))

    @staticmethod
    def event(case: TriageCase, payload: UrgenciaEstimada, *, node_id: str, seq: int) -> Event:
        return Event(
            origin_node=node_id,
            seq=seq,
            category=EventCategory.DERIVADO,
            type="urgencia_estimada",
            payload=payload.model_dump(mode="json"),
            refs=(case.urgency_event.id,),
            received_at=case.now,
        )

    def run(self, case: TriageCase) -> UrgenciaEstimada:
        """Investiga el caso. Nunca lanza: ante cualquier falla, "ambiguo, revisar"."""
        state: _State = {
            "case": case,
            "messages": [Message("system", SYSTEM_PROMPT), Message("user", _case_prompt(case))],
            "steps": [],
            "calls": 0,
            "started": self.clock(),
            "verify_now": False,
            "reply": None,
            "outcome": None,
        }
        try:
            final = self._graph.invoke(state, {"recursion_limit": 4 * self.max_steps + 4})
            steps, outcome = list(final["steps"]), final["outcome"]
            verify_now = final["verify_now"]
        except Exception as exc:  # el agente nunca tumba el nodo
            logger.exception("%s: falló el ciclo del agente", case.report.report_id)
            steps, verify_now = [], False
            outcome = self._review(case, f"falla interna del agente ({type(exc).__name__})")
        if outcome is None:
            outcome = self._review(case, "el ciclo terminó sin decisión")
        steps.append(_short(outcome.step))
        self._log(case, outcome.step)
        u = case.urgency
        return UrgenciaEstimada(
            report_id=u.report_id,
            level=apply_floor(u.floor, outcome.level),
            floor=u.floor,
            source="agente",
            probabilities=u.probabilities,
            rules=u.rules,
            risk_signals=u.risk_signals,
            verify_now=u.verify_now or verify_now,
            ambiguous=outcome.ambiguous,
            steps=tuple(steps),
        )

    # --- Nodos del grafo ------------------------------------------------------------------

    def _build_graph(self):
        graph = StateGraph(_State)
        graph.add_node("llm", self._llm_node)
        graph.add_node("herramienta", self._tool_node)
        graph.add_node("decidir", self._decide_node)
        graph.add_edge(START, "llm")
        graph.add_conditional_edges("llm", self._route, ["herramienta", "decidir", END])
        graph.add_conditional_edges("herramienta", self._after_tool, ["llm", END])
        graph.add_edge("decidir", END)
        return graph.compile()

    @staticmethod
    def _review(case: TriageCase, why: str) -> _Outcome:
        return _Outcome(case.urgency.level, True, f"ambiguo, revisar: {why}")

    def _log(self, case: TriageCase, step: str) -> None:
        logger.info("%s · %s", case.report.report_id, step)

    def _step(self, state: _State, text: str) -> list[str]:
        self._log(state["case"], text)
        return [*state["steps"], _short(text)]

    def _llm_node(self, state: _State) -> dict:
        case = state["case"]
        if state["calls"] >= self.max_steps:
            return {"outcome": self._review(case, f"se agotaron los {self.max_steps} pasos")}
        if self.clock() - state["started"] > self.time_budget_s:
            return {"outcome": self._review(case, "se agotó el tiempo del caso")}
        started = self.clock()
        try:
            reply = self.llm.complete(state["messages"], TOOLS)
        except LLMUnavailable as exc:
            return {"outcome": self._review(case, f"LLM no disponible ({exc})")}
        except Exception as exc:
            logger.exception("%s: el proveedor de LLM falló", case.report.report_id)
            return {"outcome": self._review(case, f"el LLM falló ({type(exc).__name__})")}
        who = getattr(self.llm, "last_used", None) or self.llm.name
        took = f"{self.clock() - started:.1f} s"
        calls = state["calls"] + 1
        if reply.final is not None:
            steps = self._step(
                state, f"paso {calls} · {who} ({took}): propone {_args(reply.final)}"
            )
            return {"reply": reply, "calls": calls, "steps": steps}
        text = f"paso {calls} · {who} ({took}): {reply.tool_name} {_args(reply.tool_args)}"
        if reply.tool_name not in TOOL_NAMES:
            why = f"pidió una herramienta no permitida: {reply.tool_name!r}"
            steps = self._step(state, text)
            return {"calls": calls, "steps": steps, "outcome": self._review(case, why)}
        request = json.dumps({"tool": reply.tool_name, "args": reply.tool_args}, ensure_ascii=False)
        return {
            "reply": reply,
            "calls": calls,
            "steps": self._step(state, text),
            "messages": [*state["messages"], Message("assistant", request)],
        }

    @staticmethod
    def _route(state: _State) -> str:
        reply = state["reply"]
        if state["outcome"] is not None or reply is None:
            return END
        if reply.final is not None or reply.tool_name == "fijar_urgencia":
            return "decidir"
        return "herramienta"

    @staticmethod
    def _after_tool(state: _State) -> str:
        return END if state["outcome"] is not None else "llm"

    def _tool_node(self, state: _State) -> dict:
        case, reply = state["case"], state["reply"]
        update: dict = {}
        try:
            if reply.tool_name == "incidentes_cercanos":
                minutos = reply.tool_args.get("minutos", NEARBY_DEFAULT_MIN)
                if set(reply.tool_args) - {"minutos"}:
                    raise ToolError(f"argumentos desconocidos: {sorted(reply.tool_args)}")
                found = incidentes_cercanos(case, minutos)
                result = json.dumps(found, ensure_ascii=False)
                summary = f"{len(found)} incidente(s) cercano(s)" + "".join(
                    f"; {f['titulo']} ({f['nivel']}, {f['reportes']} rep.)" for f in found[:3]
                )
            elif reply.tool_name == "radio_en_zona":
                minutos = reply.tool_args.get("minutos", NEARBY_DEFAULT_MIN)
                if set(reply.tool_args) - {"minutos"}:
                    raise ToolError(f"argumentos desconocidos: {sorted(reply.tool_args)}")
                heard = radio_en_zona(case, minutos)
                result = json.dumps(heard, ensure_ascii=False)
                summary = f"{len(heard)} reporte(s) de radio en la zona" + "".join(
                    f"; «{h['texto'][:60]}» "
                    f"({'directo' if h['lo_vieron_en_el_sitio'] else 'segunda mano'})"
                    for h in heard[:2]
                )
            else:  # marcar_verificar_ya
                motivo = reply.tool_args.get("motivo")
                if not isinstance(motivo, str) or not motivo.strip():
                    raise ToolError("marcar_verificar_ya necesita un motivo")
                update["verify_now"] = True
                result, summary = (
                    "listo: el caso queda para verificar ya",
                    f"verificar ya: {motivo}",
                )
        except ToolError as exc:
            return {
                "outcome": self._review(case, f"argumentos inválidos para {reply.tool_name}: {exc}")
            }
        return {
            **update,
            "messages": [*state["messages"], Message("tool", result)],
            "steps": self._step(state, f"{reply.tool_name} → {summary}"),
        }

    def _decide_node(self, state: _State) -> dict:
        case, reply = state["case"], state["reply"]
        answer = reply.final if reply.final is not None else reply.tool_args
        level, reason = answer.get("level"), answer.get("reason")
        quote, verify_now = answer.get("quote"), answer.get("verify_now", False)
        if not isinstance(reason, str) or not reason.strip():
            return {"outcome": self._review(case, "respuesta sin motivo")}
        if quote is not None and not isinstance(quote, str):
            return {"outcome": self._review(case, "la cita no es texto")}
        if not isinstance(verify_now, bool):
            return {"outcome": self._review(case, "verify_now no es verdadero/falso")}
        accepted, description = decide_level(
            case.urgency.floor, case.urgency.level, level, quote, case.report.text
        )
        if accepted is None:
            return {"outcome": self._review(case, description)}
        text = f"decisión: {description}; motivo: {reason.strip()}"
        if quote:
            text += f"; cita: «{quote.strip()}»"
        return {
            "verify_now": state["verify_now"] or verify_now,
            "outcome": _Outcome(accepted, False, text),
        }
