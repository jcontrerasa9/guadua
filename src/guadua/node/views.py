"""Tarjetas del tablero a partir del registro de eventos (GET /api/incidents y su detalle).

No guarda nada: cada consulta recorre los eventos y arma las tarjetas con los modelos de
node/api.py. Reglas:
- **Nivel de un reporte:** vale la urgencia más reciente de su cadena de `refs`. Una
  urgencia queda **reemplazada** si otra la referencia (el agente de triaje, N8, publica la
  suya con `refs` a la de N7 y puede bajar la sugerencia del clasificador). Si quedan varias
  sin reemplazar (conclusiones paralelas tras una partición), se conserva la más alta
  (diseño §9). Nunca queda bajo el piso más alto visto (regla 1).
- **Nivel del incidente:** el mayor de sus reportes.
- **Sin triaje todavía** (aún no llega la `urgencia_estimada` de N7, o el triaje falló): el
  incidente se muestra como crítico, de forma provisional y avisándolo en los pasos. No se
  guarda ningún evento.
- Una urgencia en la zona gris (`ambiguous`) lo dice en los pasos: "ambiguo, revisar".
- Un reporte que la correlación todavía no agrupó es su propio incidente, con su report_id.
- Estado: sin ficha · pendiente · aprobada / editada / rechazada · conflicto si hay dos
  decisiones distintas sobre la misma ficha.
- **Tras una partición** (N12) dos nodos pueden haber agrupado o fichado el mismo caso. Un
  reporte va solo en el primer incidente que lo agrupó, y una ficha anterior sin decisión
  (paralela, no replaneada) se muestra en los pasos; vale la más reciente.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from guadua.classifier.urgency import Urgency
from guadua.node.api import AgentStep, IncidentCard, IncidentDetail
from guadua.node.pipeline import normalize
from guadua.protocol.header import Incident, People
from guadua.store.event_types import (
    Decision,
    Ficha,
    IncidenteAgrupado,
    Reporte,
    ReporteNormalizado,
    UrgenciaEstimada,
    event_id_str,
)
from guadua.store.events import Event, EventId

PROVISIONAL_LEVEL = Urgency.CRITICA
PROVISIONAL_STEP = AgentStep(
    agent="triaje", detail="sin triaje todavía: se muestra como crítico por precaución"
)

INCIDENT_TITLES = {
    Incident.ATRAPADOS: "Personas atrapadas",
    Incident.COLAPSO: "Colapso",
    Incident.INCENDIO: "Incendio",
    Incident.FUGA_GAS: "Fuga de gas",
    Incident.HERIDO: "Herido",
    Incident.VIA_BLOQUEADA: "Vía bloqueada",
    Incident.OTRO: "Otro incidente",
}
PEOPLE_LABELS = {
    People.UNA: "1",
    People.DE_2_A_5: "2 a 5",
    People.DE_6_A_20: "6 a 20",
    People.MAS_DE_20: "más de 20",
}
RISK_LABELS = {"incendio": "fuego", "gas_o_cables": "gas o cables caídos"}
STATUS_BY_ACTION = {"aprobar": "aprobada", "editar": "editada", "rechazar": "rechazada"}


def _key(event: Event) -> tuple[float, str, int]:
    """Orden para decidir "el más reciente": hora de recepción y luego el id."""
    return event.received_at, event.origin_node, event.seq


@dataclass
class _Report:
    reporte: Event
    normalized: Event | None = None
    urgencies: list[Event] = field(default_factory=list)

    @property
    def data(self) -> ReporteNormalizado:
        if self.normalized is not None:
            return ReporteNormalizado.model_validate(self.normalized.payload)
        return normalize(Reporte.model_validate(self.reporte.payload))  # aún no llegó


@dataclass
class _Log:
    reports: dict[str, _Report] = field(default_factory=dict)
    incidents: dict[str, Event] = field(default_factory=dict)
    fichas: dict[str, list[Event]] = field(default_factory=lambda: defaultdict(list))
    decisions: dict[EventId, list[Event]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def read(cls, events: Iterable[Event]) -> _Log:
        log = cls()
        derived: list[Event] = []
        for event in events:
            match event.type:
                case "reporte":
                    rid = event.payload["report_id"]
                    known = log.reports.get(rid)
                    if known is None or _key(event) < _key(known.reporte):
                        log.reports[rid] = _Report(event)
                case "reporte_normalizado" | "urgencia_estimada":
                    derived.append(event)
                case "incidente_agrupado":
                    iid = event.payload["incident_id"]
                    if iid not in log.incidents or _key(event) > _key(log.incidents[iid]):
                        log.incidents[iid] = event
                case "ficha":
                    log.fichas[event.payload["incident_id"]].append(event)
                case "decision" if event.refs:
                    log.decisions[event.refs[0]].append(event)
        for event in derived:  # después de los reportes, que pueden llegar en otro orden
            report = log.reports.get(event.payload["report_id"])
            if report is None:
                continue
            if event.type == "urgencia_estimada":
                report.urgencies.append(event)
            elif report.normalized is None or _key(event) > _key(report.normalized):
                report.normalized = event
        return log

    def groups(self) -> dict[str, tuple[Event | None, list[_Report]]]:
        """incident_id → (último incidente_agrupado o None, reportes)."""
        groups: dict[str, tuple[Event | None, list[_Report]]] = {}
        claimed: set[str] = set()
        for iid, event in sorted(self.incidents.items(), key=lambda item: _key(item[1])):
            members = [
                self.reports[r]
                for r in event.payload["report_ids"]
                if r in self.reports and r not in claimed  # ya está en otro (partición)
            ]
            if members:
                groups[iid] = (event, members)
                claimed.update(event.payload["report_ids"])
        for rid, report in self.reports.items():
            if rid not in claimed:
                groups[rid] = (None, [report])
        return groups


def _current(urgencies: list[Event]) -> list[UrgenciaEstimada]:
    """Las urgencias de un reporte que ninguna otra reemplaza (ninguna las referencia)."""
    replaced = {ref for u in urgencies for ref in u.refs}
    return [UrgenciaEstimada.model_validate(u.payload) for u in urgencies if u.id not in replaced]


def _report_level(urgencies: list[Event]) -> Urgency:
    """La más alta de las vigentes, sin bajar nunca del piso más alto visto."""
    floor = max(Urgency(u.payload["floor"]) for u in urgencies)
    return max([floor, *(u.level for u in _current(urgencies))])


def _title(data: ReporteNormalizado) -> str:
    title = INCIDENT_TITLES[data.incident]
    people = PEOPLE_LABELS.get(data.people)
    if people and "atrapados" in data.vital_signs and data.incident != Incident.ATRAPADOS:
        return f"{title} con {people} atrapados"
    if people:
        return f"{title} · {people} {'persona' if people == '1' else 'personas'}"
    return title


def _status(ficha: Event | None, decisions: list[Event]) -> str:
    if ficha is None:
        return "sin_ficha"
    actions = {d.payload["action"] for d in decisions}
    if not actions:
        return "pendiente"
    if len(actions) > 1:
        return "conflicto"
    return STATUS_BY_ACTION[actions.pop()]


def _suggestion(ficha: Ficha) -> str:
    if ficha.responsible is None:
        return "Sin responsable identificado: escalar al coordinador"
    if ficha.units_available is None:
        return ficha.responsible
    return f"{ficha.responsible} ({ficha.units_available} unidades libres)"


def _build(iid: str, incident_event: Event | None, members: list[_Report], log: _Log) -> dict:
    members = sorted(members, key=lambda r: _key(r.reporte))
    first = members[0].data
    urgencies = sorted((u for r in members for u in r.urgencies), key=_key)
    parsed = [UrgenciaEstimada.model_validate(u.payload) for u in urgencies]
    current = [c for r in members for c in _current(r.urgencies)]
    level = max(
        (_report_level(r.urgencies) for r in members if r.urgencies), default=PROVISIONAL_LEVEL
    )
    group = IncidenteAgrupado.model_validate(incident_event.payload) if incident_event else None

    fichas = sorted(log.fichas.get(iid, []), key=_key)
    ficha_event = fichas[-1] if fichas else None
    ficha = Ficha.model_validate(ficha_event.payload) if ficha_event else None
    decisions = sorted(log.decisions.get(ficha_event.id, []), key=_key) if ficha_event else []
    # Fichas anteriores que el comandante rechazó: la actual es su replaneación.
    rejected = [
        (old, decision)
        for old in fichas[:-1]
        for decision in sorted(log.decisions.get(old.id, []), key=_key)
        if decision.payload["action"] == "rechazar"
    ]
    suggestion = _suggestion(ficha) if ficha else None
    if suggestion and rejected:
        names = dict.fromkeys(
            old.payload["responsible"] or "sin responsable" for old, _ in rejected
        )
        suggestion += " · replaneada: se rechazó " + ", ".join(names)
    status = _status(ficha_event, decisions)
    message = ficha.message if ficha else None
    if status == "editada":
        message = Decision.model_validate(decisions[-1].payload).edited_message

    risks = sorted({s for r in members for s in r.data.risk_signals})
    risk_warning = (ficha.risk_warning if ficha else None) or (
        "Riesgo en la escena: " + ", ".join(RISK_LABELS.get(s, s) for s in risks) if risks else None
    )
    related = [r.reporte for r in members] + [e for r in members if (e := r.normalized)]
    related += (
        urgencies
        + fichas
        + decisions
        + [d for _, d in rejected]
        + ([incident_event] if incident_event else [])
    )

    steps: list[AgentStep] = []
    if not parsed:
        steps.append(PROVISIONAL_STEP)
    for u in parsed:
        detail = f"{u.source}: nivel {u.level.name.lower()}"
        detail += "".join(f"; {r}" for r in u.rules) + ("; ambiguo, revisar" if u.ambiguous else "")
        steps.append(AgentStep(agent="triaje", detail=detail))
        steps.extend(AgentStep(agent="triaje", detail=f"agente · {s}") for s in u.steps)
    if group:
        detail = f"vínculo {group.link}, confianza {group.confidence:.2f}"
        steps.append(AgentStep(agent="correlacion", detail=detail))
    for old, decision in rejected:
        who = old.payload["responsible"] or "sin responsable"
        detail = f"ficha {event_id_str(old)} ({who}) rechazada por {decision.payload['operator']}"
        steps.append(AgentStep(agent="coordinacion", detail=detail))
    for old in fichas[:-1]:
        if not log.decisions.get(old.id):
            detail = f"ficha paralela {event_id_str(old)} sin decisión: vale la más reciente"
            steps.append(AgentStep(agent="coordinacion", detail=detail))
    if ficha:
        origin = "plantilla" if ficha.from_template else "redactada con LLM"
        detail = f"ficha {event_id_str(ficha_event)}: {_suggestion(ficha)}; {origin}"
        steps.append(AgentStep(agent="coordinacion", detail=detail))

    top = max(current, key=lambda u: u.level, default=None)
    return {
        "incident_id": iid,
        "level": level,
        "incident": first.incident,
        "title": _title(first),
        "location": first.location,
        "confidence": group.confidence if group else None,
        "rumor": group.rumor if group else False,
        "verify_now": any(u.verify_now for u in current),
        "risk_warning": risk_warning,
        "suggestion": suggestion,
        "message": message,
        "status": status,
        "ficha_id": event_id_str(ficha_event) if ficha_event else None,
        "report_count": len(members),
        "received_at": min(r.reporte.received_at for r in members),
        "updated_at": max(e.received_at for e in related),
        # Solo para el detalle:
        "report_ids": tuple(r.data.report_id for r in members),
        "rules": tuple(dict.fromkeys(rule for u in parsed for rule in u.rules)),
        "probabilities": dict(top.probabilities) if top else {},
        "sources": tuple(
            "ciudadano directo" if r.data.direct_observation else "ciudadano de segunda mano"
            for r in members
        ),
        "contradictions": group.contradictions if group else (),
        "citations": ficha.citations if ficha else (),
        "steps": tuple(steps),
    }


_DETAIL_ONLY = set(IncidentDetail.model_fields) - set(IncidentCard.model_fields)


def incident_cards(events: Iterable[Event]) -> list[IncidentCard]:
    """Tarjetas ordenadas por nivel (mayor primero) y luego por antigüedad."""
    log = _Log.read(events)
    cards = [
        IncidentCard(
            **{k: v for k, v in _build(iid, ev, members, log).items() if k not in _DETAIL_ONLY}
        )
        for iid, (ev, members) in log.groups().items()
    ]
    return sorted(cards, key=lambda c: (-c.level, c.received_at, c.incident_id))


def incident_details(events: Iterable[Event]) -> list[IncidentDetail]:
    """Todos los incidentes con su detalle (lo usa la herramienta `incidentes_cercanos`)."""
    log = _Log.read(events)
    return [
        IncidentDetail(**_build(iid, ev, members, log))
        for iid, (ev, members) in log.groups().items()
    ]


def incident_detail(events: Iterable[Event], incident_id: str) -> IncidentDetail | None:
    log = _Log.read(events)
    group = log.groups().get(incident_id)
    if group is None:
        return None
    return IncidentDetail(**_build(incident_id, group[0], group[1], log))
