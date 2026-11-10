"""Agente 7 · Vigía: monitoreo de casos rezagados y generación de solicitudes (D6).

Diseño: docs/diseno.md §10 y §3.2. Ficha: docs/fichas/vigia.md.
Plan: docs/plan.md (tarea D6).

Objetivo:
- Inspecciona por iniciativa propia la lista de incidentes abiertos sin atender,
  ordenados por antigüedad (el más viejo primero).
- Detecta si ha llegado evidencia nueva desde la última acción registrada.
- Genera solicitudes hacia triaje (reevaluación ante nueva evidencia o ambigüedad),
  hacia coordinación (casos sin ficha o demoras de respuesta) o alertas hacia el tablero
  para casos críticos desatendidos.

INVARIANTE INVIOLABLE (Regla 1 de AGENTS.md):
- El Vigía NUNCA cambia prioridades: no puede modificar el nivel de urgencia (`level`)
  ni el piso determinista (`floor`) de ningún reporte o incidente. Solo emite solicitudes
  y alertas justificadas para que los agentes respectivos o el operador humano actúen.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from guadua.classifier.urgency import Urgency
from guadua.node.views import IncidentDetail, incident_details
from guadua.providers.llm import LLMProvider, LLMUnavailable, Message
from guadua.store.event_types import SolicitudVigia
from guadua.store.events import Event, EventCategory, EventStore

# Umbrales por defecto en segundos
DEFAULT_UMBRAL_OLVIDO_S: float = 900.0  # 15 minutos para caso rezagado
DEFAULT_UMBRAL_CRITICO_S: float = 300.0  # 5 minutos para alertar casos críticos/altos


@dataclass(frozen=True, slots=True)
class Solicitud:
    """Solicitud o alerta emitida por el Agente Vigía hacia otro agente o el tablero."""

    incident_id: str
    target_agent: Literal["triaje", "coordinacion", "tablero"]
    action: Literal["reevaluacion_triaje", "crear_ficha", "revisar_demora", "alerta_olvido"]
    reason: str
    incident_level: Urgency
    age_seconds: float
    has_new_evidence: bool = False
    new_reports_count: int = 0
    timestamp: float = 0.0


@dataclass(frozen=True, slots=True)
class CasoSinAtender:
    """Resumen de un incidente abierto que requiere atención o seguimiento."""

    incident_id: str
    level: Urgency
    status: str
    first_seen: float
    last_updated: float
    age_seconds: float
    report_ids: tuple[str, ...]
    has_ficha: bool
    ficha_pending: bool
    is_ambiguous: bool
    verify_now: bool
    last_action: float = 0.0
    new_evidence_count: int = 0


# --- Herramientas de la lista blanca (§10) ---------------------------------------------


def _obtener_eventos(store_or_events: EventStore | Iterable[Event]) -> list[Event]:
    """Extrae la lista de eventos desde un EventStore o iterable."""
    if hasattr(store_or_events, "since"):
        return list(store_or_events.since({}))  # type: ignore[union-attr]
    return list(store_or_events)


def incidentes_sin_atender(
    store_or_events: EventStore | Iterable[Event],
    now: float | None = None,
    threshold_seconds: float = DEFAULT_UMBRAL_OLVIDO_S,
) -> list[CasoSinAtender]:
    """Herramienta: Lista de incidentes abiertos sin atender, ordenados por antigüedad.

    Criterios de desatención:
    - Estado `sin_ficha`: tiene reportes pero coordinación aún no ha formulado ficha.
    - Estado `pendiente`: tiene ficha formulada pero lleva tiempo sin decisión del operador.
    - Estado `rechazada`: la ficha anterior fue rechazada y no se ha emitido una nueva.
    - Indicador `verify_now=True` o pasos con `ambiguo, revisar` sin confirmación en campo.

    Orden: del más antiguo al más reciente (según received_at del primer reporte).
    """
    events = _obtener_eventos(store_or_events)
    if not events:
        return []

    current_time = now if now is not None else time.time()
    details: list[IncidentDetail] = incident_details(events)

    sin_atender: list[CasoSinAtender] = []
    estados_desatendidos = {"sin_ficha", "pendiente", "rechazada", "conflicto"}

    for detail in details:
        es_desatendido = detail.status in estados_desatendidos or detail.verify_now
        is_ambiguous = any("ambiguo, revisar" in s.detail for s in detail.steps)
        if is_ambiguous:
            es_desatendido = True

        if not es_desatendido:
            continue

        age_s = max(0.0, current_time - detail.received_at)
        if threshold_seconds > 0 and age_s < threshold_seconds:
            continue

        rids = set(detail.report_ids) | {detail.incident_id}
        ficha_ids = {
            f.id
            for f in events
            if f.type == "ficha" and f.payload.get("incident_id") == detail.incident_id
        }
        action_timestamps = [
            ev.received_at
            for ev in events
            if (ev.type == "urgencia_estimada" and ev.payload.get("report_id") in rids)
            or (ev.id in ficha_ids)
            or (ev.type == "decision" and any(ref in ficha_ids for ref in ev.refs))
        ]
        last_action = max(action_timestamps, default=detail.received_at)

        sin_atender.append(
            CasoSinAtender(
                incident_id=detail.incident_id,
                level=detail.level,
                status=detail.status,
                first_seen=detail.received_at,
                last_updated=detail.updated_at,
                age_seconds=age_s,
                report_ids=detail.report_ids,
                has_ficha=detail.status != "sin_ficha",
                ficha_pending=detail.status == "pendiente",
                is_ambiguous=is_ambiguous,
                verify_now=detail.verify_now,
                last_action=last_action,
            )
        )

    # Ordenar por antigüedad estricta (el más antiguo primero)
    return sorted(sin_atender, key=lambda c: (c.first_seen, c.incident_id))


def evidencia_desde(
    store_or_events: EventStore | Iterable[Event],
    incident_id: str,
    since_timestamp: float,
) -> list[Event]:
    """Herramienta: Retorna evidencia nueva para el incidente posterior a since_timestamp."""
    events = _obtener_eventos(store_or_events)
    nuevos: list[Event] = []

    # Recopilar reportes del incidente
    report_ids_incident: set[str] = {incident_id}
    for ev in events:
        if ev.type == "incidente_agrupado" and ev.payload.get("incident_id") == incident_id:
            report_ids_incident.update(ev.payload.get("report_ids", ()))

    for ev in events:
        if ev.received_at <= since_timestamp:
            continue

        # Reportes nuevos asociados al incidente
        rid = ev.payload.get("report_id")
        es_reporte = (
            ev.type in ("reporte", "reporte_normalizado")
            and bool(rid)
            and (rid in report_ids_incident or incident_id == rid)
        )
        # Transcripciones de radio o confirmaciones de campo
        es_radio_campo = (
            ev.type in ("transcripcion_radio", "confirmacion_campo")
            and ev.payload.get("incident_id") == incident_id
        )
        if es_reporte or es_radio_campo:
            nuevos.append(ev)

    return nuevos


def solicitar_reevaluacion(
    incident_id: str,
    target: Literal["triaje", "coordinacion"],
    reason: str,
    level: Urgency,
    age_seconds: float,
    *,
    has_new_evidence: bool = False,
    new_reports_count: int = 0,
    timestamp: float = 0.0,
) -> Solicitud:
    """Herramienta: Construye una solicitud de reevaluación hacia triaje o coordinación."""
    action: Literal["reevaluacion_triaje", "crear_ficha"] = (
        "reevaluacion_triaje" if target == "triaje" else "crear_ficha"
    )
    return Solicitud(
        incident_id=incident_id,
        target_agent=target,
        action=action,
        reason=reason,
        incident_level=level,
        age_seconds=age_seconds,
        has_new_evidence=has_new_evidence,
        new_reports_count=new_reports_count,
        timestamp=timestamp,
    )


def alertar_tablero(
    incident_id: str,
    reason: str,
    level: Urgency,
    age_seconds: float,
    *,
    timestamp: float = 0.0,
) -> Solicitud:
    """Herramienta: Construye una alerta de olvido dirigida al tablero del comandante."""
    return Solicitud(
        incident_id=incident_id,
        target_agent="tablero",
        action="alerta_olvido",
        reason=reason,
        incident_level=level,
        age_seconds=age_seconds,
        has_new_evidence=False,
        new_reports_count=0,
        timestamp=timestamp,
    )


# --- Agente Vigía ----------------------------------------------------------------------


class AgenteVigia:
    """Agente autónomo que previene el olvido de incidentes sin alterar prioridades."""

    def __init__(
        self,
        store: EventStore,
        llm: LLMProvider | None = None,
        *,
        clock: Callable[[], float] | None = None,
        umbral_olvido_s: float = DEFAULT_UMBRAL_OLVIDO_S,
        umbral_critico_s: float = DEFAULT_UMBRAL_CRITICO_S,
    ) -> None:
        self.store = store
        self.llm = llm
        self.clock = clock if clock is not None else time.time
        self.umbral_olvido_s = umbral_olvido_s
        self.umbral_critico_s = umbral_critico_s

    def revisar_casos_olvidados(self, now: float | None = None) -> list[Solicitud]:
        """Escanea el almacén de eventos y genera solicitudes y alertas para casos desatendidos.

        Garantiza de forma absoluta que NO modifica ninguna urgencia ni piso (Regla 1).
        """
        current_time = now if now is not None else self.clock()
        solicitudes: list[Solicitud] = []

        # 1. Obtener incidentes sin atender ordenados por antigüedad
        casos = incidentes_sin_atender(self.store, now=current_time, threshold_seconds=0.0)

        for caso in casos:
            # Comprobar si supera umbral general de olvido o el umbral crítico
            es_critico = caso.level in (Urgency.CRITICA, Urgency.ALTA)
            umbral_aplicable = self.umbral_critico_s if es_critico else self.umbral_olvido_s

            # Buscar evidencia nueva desde su última acción registrada (§10)
            ev_nuevas = evidencia_desde(self.store, caso.incident_id, caso.last_action)
            report_ids_nuevos = {
                ev.payload.get("report_id")
                for ev in ev_nuevas
                if ev.type in ("reporte", "reporte_normalizado") and ev.payload.get("report_id")
            }
            new_count = len(report_ids_nuevos) or len(ev_nuevas)
            has_new_evidence = len(ev_nuevas) > 0

            # Si el caso no supera el umbral y no tiene evidencia nueva, no es olvidado
            if caso.age_seconds < umbral_aplicable and not has_new_evidence:
                continue

            minutos = int(caso.age_seconds // 60)

            # A. Solicitud a Triaje: si llegó nueva evidencia o el caso es ambiguo sin resolver
            if has_new_evidence or caso.is_ambiguous or caso.verify_now:
                motivo_triaje = self._redactar_motivo(
                    target="triaje",
                    caso=caso,
                    minutos=minutos,
                    new_count=new_count,
                    has_new_evidence=has_new_evidence,
                )
                solicitudes.append(
                    solicitar_reevaluacion(
                        incident_id=caso.incident_id,
                        target="triaje",
                        reason=motivo_triaje,
                        level=caso.level,
                        age_seconds=caso.age_seconds,
                        has_new_evidence=has_new_evidence,
                        new_reports_count=new_count,
                        timestamp=current_time,
                    )
                )

            # B. Solicitud a Coordinación: si no tiene ficha o está pendiente de decisión
            if caso.status == "sin_ficha":
                motivo_coord = self._redactar_motivo(
                    target="coordinacion",
                    caso=caso,
                    minutos=minutos,
                    new_count=new_count,
                    has_new_evidence=has_new_evidence,
                )
                solicitudes.append(
                    solicitar_reevaluacion(
                        incident_id=caso.incident_id,
                        target="coordinacion",
                        reason=motivo_coord,
                        level=caso.level,
                        age_seconds=caso.age_seconds,
                        has_new_evidence=has_new_evidence,
                        new_reports_count=new_count,
                        timestamp=current_time,
                    )
                )
            elif caso.status == "pendiente" and caso.age_seconds >= umbral_aplicable:
                motivo_demora = (
                    f"Ficha de incidente {caso.incident_id} ({caso.level.name.lower()}) lleva "
                    f"{minutos} min pendiente de aprobación por el operador humano."
                )
                solicitudes.append(
                    Solicitud(
                        incident_id=caso.incident_id,
                        target_agent="coordinacion",
                        action="revisar_demora",
                        reason=motivo_demora,
                        incident_level=caso.level,
                        age_seconds=caso.age_seconds,
                        has_new_evidence=has_new_evidence,
                        new_reports_count=new_count,
                        timestamp=current_time,
                    )
                )

            # C. Alerta al Tablero: casos críticos o altos que superan el umbral de tiempo
            if es_critico and caso.age_seconds >= self.umbral_critico_s:
                motivo_alerta = (
                    f"ALERTA DE OLVIDO: Incidente {caso.incident_id} de urgencia "
                    f"{caso.level.name.lower()} lleva {minutos} min sin despacho aprobado."
                )
                solicitudes.append(
                    alertar_tablero(
                        incident_id=caso.incident_id,
                        reason=motivo_alerta,
                        level=caso.level,
                        age_seconds=caso.age_seconds,
                        timestamp=current_time,
                    )
                )

        return solicitudes

    def _redactar_motivo(
        self,
        target: Literal["triaje", "coordinacion"],
        caso: CasoSinAtender,
        minutos: int,
        new_count: int,
        has_new_evidence: bool,
    ) -> str:
        """Genera el motivo explicativo utilizando LLM si está disponible o plantilla."""
        plantilla = self._motivo_plantilla(target, caso, minutos, new_count, has_new_evidence)

        if self.llm is None:
            return plantilla

        try:
            prompt = (
                f"Eres el Agente Vigía de emergencias en Manizales. Redacta en 1 sola frase "
                f"concisa y formal la justificación de una solicitud a {target}.\n"
                f"Incidente: {caso.incident_id}, nivel: {caso.level.name.lower()}, "
                f"tiempo desatendido: {minutos} minutos, reportes nuevos: {new_count}, "
                f"ambiguo: {caso.is_ambiguous}.\n"
                f'Responde con un JSON: {{"motivo": "..."}}.'
            )
            reply = self.llm.complete(
                messages=[
                    Message(role="system", content="Eres el Agente Vigía del sistema Guadua."),
                    Message(role="user", content=prompt),
                ],
                tools=[],
            )
            if reply.final and isinstance(reply.final, dict):
                motivo = str(
                    reply.final.get("motivo")
                    or reply.final.get("reason")
                    or reply.final.get("message")
                    or ""
                ).strip()
                if len(motivo) > 10:
                    return motivo
        except (LLMUnavailable, Exception):
            pass

        return plantilla

    def _motivo_plantilla(
        self,
        target: Literal["triaje", "coordinacion"],
        caso: CasoSinAtender,
        minutos: int,
        new_count: int,
        has_new_evidence: bool,
    ) -> str:
        """Plantilla determinista sin LLM."""
        if target == "triaje":
            if has_new_evidence:
                return (
                    f"Incidente {caso.incident_id} ({caso.level.name.lower()}) recibió {new_count} "
                    f"reportes nuevos tras {minutos} min; requiere reevaluación de triaje."
                )
            if caso.is_ambiguous:
                return (
                    f"Incidente {caso.incident_id} permanece ambiguo tras {minutos} min "
                    f"sin resolución; requiere reevaluación."
                )
            return (
                f"Incidente {caso.incident_id} sin atención tras {minutos} min; "
                f"requiere verificación de urgencia."
            )

        # Para coordinación
        if has_new_evidence:
            return (
                f"Incidente {caso.incident_id} ({caso.level.name.lower()}) sin ficha tras "
                f"{minutos} min y con {new_count} reportes nuevos; requiere formulación EMRE."
            )
        return (
            f"Incidente {caso.incident_id} ({caso.level.name.lower()}) sin ficha tras "
            f"{minutos} min; requiere asignación de responsable y recursos según matriz EMRE."
        )


def solicitud_event(
    solicitud: Solicitud, incidente: Event, *, node_id: str, seq: int, received_at: float
) -> Event:
    """La solicitud como evento `solicitud_vigia` (D6), con el `incidente_agrupado` que se
    revisó en `refs[0]`. Así queda en el registro, viaja por gossip y se ve en el tablero."""
    payload = SolicitudVigia(
        incident_id=solicitud.incident_id,
        target=solicitud.target_agent,
        action=solicitud.action,
        reason=solicitud.reason,
        level=solicitud.incident_level,
        age_s=max(0.0, solicitud.age_seconds),
        new_reports=solicitud.new_reports_count,
    )
    return Event(
        origin_node=node_id,
        seq=seq,
        category=EventCategory.DERIVADO,
        type="solicitud_vigia",
        payload=payload.model_dump(mode="json"),
        refs=(incidente.id,),
        received_at=received_at,
    )
