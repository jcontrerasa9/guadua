"""Pruebas del Agente 7 Vigía (D6).

Verifica:
- Lista blanca de herramientas:
  * incidentes_sin_atender: detecta casos abiertos y los ordena por antigüedad.
  * evidencia_desde: detecta reportes y transcripciones posteriores a un timestamp.
- AgenteVigia.revisar_casos_olvidados:
  * Casos sin ficha generan solicitudes a coordinación.
  * Casos con demora de aprobación generan solicitudes a coordinación.
  * Casos con nueva evidencia o ambigüedad generan solicitudes a triaje.
  * Casos críticos o altos desatendidos generan alertas al tablero.
  * Invariante (Regla 1 de AGENTS.md): el Vigía NUNCA cambia prioridades ni pisos.
  * Funciona 100% offline sin LLM (NullLLM) y con ScriptedLLM usando razonamiento.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from guadua.agents.vigia import (
    AgenteVigia,
    alertar_tablero,
    evidencia_desde,
    incidentes_sin_atender,
    solicitar_reevaluacion,
)
from guadua.classifier.urgency import Urgency
from guadua.node.views import incident_cards
from guadua.providers.llm import LLMReply
from guadua.store.events import Event
from tests.fakes import InMemoryEventStore, NullLLM, ScriptedLLM

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "event_examples.json"
EXAMPLES = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
BASE_EVENTS = [Event.model_validate(e) for e in EXAMPLES["valid"]]
BY_TYPE = {e.type: e for e in BASE_EVENTS}


def make_report(
    rid: str,
    seq: int,
    received_at: float,
    *,
    node: str = "A",
    incident_type: int = 1,
) -> list[Event]:
    """Crea un par reporte y reporte_normalizado con IDs coherentes."""
    rep = BY_TYPE["reporte"].model_copy(
        update={
            "id": f"{node}:{seq}",
            "origin_node": node,
            "seq": seq,
            "received_at": received_at,
            "payload": {
                **BY_TYPE["reporte"].payload,
                "report_id": rid,
                "incident": incident_type,
            },
        }
    )
    norm = BY_TYPE["reporte_normalizado"].model_copy(
        update={
            "id": f"{node}:{seq + 1}",
            "origin_node": node,
            "seq": seq + 1,
            "received_at": received_at,
            "refs": (rep.id,),
            "payload": {
                **BY_TYPE["reporte_normalizado"].payload,
                "report_id": rid,
                "incident": incident_type,
            },
        }
    )
    return [rep, norm]


def make_urgency(
    rid: str,
    seq: int,
    level: int,
    received_at: float,
    *,
    floor: int | None = None,
    node: str = "A",
    source: str = "reglas",
) -> Event:
    """Crea un evento de urgencia_estimada."""
    fl = level if floor is None else floor
    return BY_TYPE["urgencia_estimada"].model_copy(
        update={
            "id": f"{node}:{seq}",
            "origin_node": node,
            "seq": seq,
            "received_at": received_at,
            "payload": {
                **BY_TYPE["urgencia_estimada"].payload,
                "report_id": rid,
                "level": level,
                "floor": fl,
                "source": source,
            },
        }
    )


def make_ficha(
    incident_id: str,
    seq: int,
    received_at: float,
    *,
    node: str = "A",
) -> Event:
    """Crea un evento de ficha de coordinación."""
    return BY_TYPE["ficha"].model_copy(
        update={
            "id": f"{node}:{seq}",
            "origin_node": node,
            "seq": seq,
            "received_at": received_at,
            "payload": {
                **BY_TYPE["ficha"].payload,
                "incident_id": incident_id,
            },
        }
    )


def make_decision(
    ficha_ref: tuple[str, int],
    action: str,
    seq: int,
    received_at: float,
    *,
    node: str = "A",
) -> Event:
    """Crea una decisión humana sobre una ficha."""
    return BY_TYPE["decision"].model_copy(
        update={
            "id": f"{node}:{seq}",
            "origin_node": node,
            "seq": seq,
            "received_at": received_at,
            "refs": (ficha_ref,),
            "payload": {
                **BY_TYPE["decision"].payload,
                "action": action,
            },
        }
    )


# --- Pruebas de herramientas (§10) ----------------------------------------------------


def test_incidentes_sin_atender_vacio() -> None:
    assert incidentes_sin_atender([]) == []
    store = InMemoryEventStore()
    assert incidentes_sin_atender(store) == []


def test_incidentes_sin_atender_orden_antiguedad_y_estados() -> None:
    store = InMemoryEventStore()
    # Caso 1: antiguo (t=100.0), sin ficha, urgencia alta
    for ev in make_report("11111111-1", 1, 100.0):
        store.append(ev)
    store.append(make_urgency("11111111-1", 3, Urgency.ALTA, 100.0))

    # Caso 2: intermedio (t=200.0), con ficha pendiente, urgencia media
    for ev in make_report("22222222-1", 10, 200.0):
        store.append(ev)
    store.append(make_urgency("22222222-1", 12, Urgency.MEDIA, 200.0))
    store.append(make_ficha("22222222-1", 13, 205.0))

    # Caso 3: reciente (t=500.0), aprobada (atendida)
    for ev in make_report("33333333-1", 20, 500.0):
        store.append(ev)
    store.append(make_urgency("33333333-1", 22, Urgency.CRITICA, 500.0))
    store.append(make_ficha("33333333-1", 23, 505.0))
    store.append(make_decision(("A", 23), "aprobar", 24, 510.0))

    # Caso 4: nuevo reciente (t=950.0), sin ficha
    for ev in make_report("44444444-1", 30, 950.0):
        store.append(ev)
    store.append(make_urgency("44444444-1", 32, Urgency.BAJA, 950.0))

    now = 1000.0
    # Con umbral de 300s, '44444444-1' tiene 50s de antigüedad y no debe salir si threshold=300
    casos = incidentes_sin_atender(store, now=now, threshold_seconds=300.0)
    assert len(casos) == 2
    # El más antiguo primero:
    assert casos[0].incident_id == "11111111-1"
    assert casos[0].status == "sin_ficha"
    assert casos[0].age_seconds == pytest.approx(900.0)
    assert not casos[0].has_ficha

    assert casos[1].incident_id == "22222222-1"
    assert casos[1].status == "pendiente"
    assert casos[1].age_seconds == pytest.approx(800.0)
    assert casos[1].has_ficha
    assert casos[1].ficha_pending

    # Sin umbral (0.0): entran los 3 desatendidos, el atendido ("aprobada") se excluye
    todos = incidentes_sin_atender(store, now=now, threshold_seconds=0.0)
    assert len(todos) == 3
    assert [c.incident_id for c in todos] == ["11111111-1", "22222222-1", "44444444-1"]


def test_evidencia_desde() -> None:
    store = InMemoryEventStore()
    for ev in make_report("11111111-1", 1, 100.0):
        store.append(ev)

    # Eventos anteriores a t=200
    for ev in make_report("11111111-1", 3, 150.0):
        store.append(ev)

    # Evento posterior a t=200 para 11111111-1
    for ev in make_report("11111111-1", 5, 250.0):
        store.append(ev)

    # Evento posterior a t=200 pero para otro incidente
    for ev in make_report("99999999-1", 7, 260.0):
        store.append(ev)

    nuevos = evidencia_desde(store, "11111111-1", since_timestamp=200.0)
    # Deben ser los 2 eventos de make_report("11111111-1", 5, 250.0)
    assert len(nuevos) == 2
    assert all(e.received_at > 200.0 for e in nuevos)
    assert all(e.payload.get("report_id") == "11111111-1" for e in nuevos)


def test_solicitar_reevaluacion_y_alertar_tablero() -> None:
    sol_triaje = solicitar_reevaluacion(
        incident_id="aaaaaaaa-1",
        target="triaje",
        reason="Nueva evidencia recibida",
        level=Urgency.ALTA,
        age_seconds=600.0,
        has_new_evidence=True,
        new_reports_count=2,
        timestamp=1000.0,
    )
    assert sol_triaje.target_agent == "triaje"
    assert sol_triaje.action == "reevaluacion_triaje"
    assert sol_triaje.has_new_evidence is True
    assert sol_triaje.new_reports_count == 2

    sol_coord = solicitar_reevaluacion(
        incident_id="bbbbbbbb-1",
        target="coordinacion",
        reason="Sin ficha tras 15 minutos",
        level=Urgency.MEDIA,
        age_seconds=900.0,
        timestamp=1000.0,
    )
    assert sol_coord.target_agent == "coordinacion"
    assert sol_coord.action == "crear_ficha"

    alerta = alertar_tablero(
        incident_id="cccccccc-1",
        reason="Incidente crítico olvidado",
        level=Urgency.CRITICA,
        age_seconds=700.0,
        timestamp=1000.0,
    )
    assert alerta.target_agent == "tablero"
    assert alerta.action == "alerta_olvido"
    assert alerta.incident_level == Urgency.CRITICA


# --- Pruebas del Agente Vigía ---------------------------------------------------------


def test_vigia_detecta_caso_olvidado_sin_ficha() -> None:
    """Un caso sin ficha tras el umbral genera solicitud a coordinación."""
    store = InMemoryEventStore()
    now = 2000.0
    # Creado hace 1000s (umbral por defecto 900s)
    for ev in make_report("0000aaaa-1", 1, now - 1000.0):
        store.append(ev)
    store.append(make_urgency("0000aaaa-1", 3, Urgency.MEDIA, now - 1000.0))

    vigia = AgenteVigia(store, llm=NullLLM(), clock=lambda: now)
    solicitudes = vigia.revisar_casos_olvidados()

    assert len(solicitudes) == 1
    sol = solicitudes[0]
    assert sol.incident_id == "0000aaaa-1"
    assert sol.target_agent == "coordinacion"
    assert sol.action == "crear_ficha"
    assert "sin ficha" in sol.reason.lower()


def test_vigia_detecta_demora_en_aprobacion() -> None:
    """Un caso con ficha pero sin decisión del operador tras el umbral genera revisión de demora."""
    store = InMemoryEventStore()
    now = 2000.0
    for ev in make_report("0000bbbb-1", 1, now - 1200.0):
        store.append(ev)
    store.append(make_urgency("0000bbbb-1", 3, Urgency.MEDIA, now - 1200.0))
    store.append(make_ficha("0000bbbb-1", 4, now - 1100.0))

    vigia = AgenteVigia(store, llm=NullLLM(), clock=lambda: now)
    solicitudes = vigia.revisar_casos_olvidados()

    assert any(
        s.target_agent == "coordinacion" and s.action == "revisar_demora" for s in solicitudes
    )


def test_vigia_detecta_evidencia_nueva_y_solicita_triaje() -> None:
    """Si llega un reporte nuevo tras la última actualización, solicita reevaluación a triaje."""
    store = InMemoryEventStore()
    t0 = 1000.0
    for ev in make_report("0000cccc-1", 1, t0):
        store.append(ev)
    store.append(make_urgency("0000cccc-1", 3, Urgency.MEDIA, t0))
    store.append(make_ficha("0000cccc-1", 4, t0 + 10.0))

    # Llega nuevo reporte a t0 + 50.0
    for ev in make_report("0000cccc-1", 10, t0 + 50.0):
        store.append(ev)

    # Aunque solo han pasado 60s (no superaría umbral por tiempo), la nueva evidencia activa a Vigía
    now = t0 + 60.0
    vigia = AgenteVigia(store, llm=NullLLM(), clock=lambda: now)
    solicitudes = vigia.revisar_casos_olvidados()

    triaje_sols = [s for s in solicitudes if s.target_agent == "triaje"]
    assert len(triaje_sols) == 1
    assert triaje_sols[0].action == "reevaluacion_triaje"
    assert triaje_sols[0].has_new_evidence is True
    assert triaje_sols[0].new_reports_count >= 1


def test_vigia_alerta_tablero_en_casos_criticos() -> None:
    """Casos críticos o altos que superan el umbral crítico emiten alerta al tablero."""
    store = InMemoryEventStore()
    now = 1000.0
    # Incidente crítico hace 400s (umbral crítico = 300s)
    for ev in make_report("0000dddd-1", 1, now - 400.0):
        store.append(ev)
    store.append(make_urgency("0000dddd-1", 3, Urgency.CRITICA, now - 400.0))

    vigia = AgenteVigia(store, llm=NullLLM(), clock=lambda: now)
    solicitudes = vigia.revisar_casos_olvidados()

    alertas = [s for s in solicitudes if s.target_agent == "tablero"]
    assert len(alertas) == 1
    assert alertas[0].action == "alerta_olvido"
    assert alertas[0].incident_level == Urgency.CRITICA
    assert "ALERTA DE OLVIDO" in alertas[0].reason


def test_invariante_regla_1_vigia_nunca_cambia_prioridades_ni_pisos() -> None:
    """REGLA 1 INVIOLABLE: El Vigía NUNCA altera el nivel de urgencia ni el piso."""
    store = InMemoryEventStore()
    now = 5000.0
    # Configurar incidentes con diversas urgencias y pisos
    for ev in make_report("0000eeee-1", 1, now - 1500.0):
        store.append(ev)
    store.append(
        make_urgency("0000eeee-1", 3, Urgency.CRITICA, now - 1500.0, floor=Urgency.CRITICA)
    )

    for ev in make_report("0000ffff-1", 10, now - 1200.0):
        store.append(ev)
    store.append(make_urgency("0000ffff-1", 12, Urgency.ALTA, now - 1200.0, floor=Urgency.MEDIA))

    for ev in make_report("00001111-1", 20, now - 1000.0):
        store.append(ev)
    store.append(make_urgency("00001111-1", 22, Urgency.MEDIA, now - 1000.0, floor=Urgency.BAJA))

    # Captura de tarjetas antes de ejecutar vigía
    tarjetas_antes = incident_cards(store.since({}))
    niveles_antes = {c.incident_id: c.level for c in tarjetas_antes}
    conteo_eventos_antes = len(store.since({}))
    urgencias_antes = [
        (e.payload["report_id"], e.payload["level"], e.payload["floor"])
        for e in store.since({})
        if e.type == "urgencia_estimada"
    ]

    # Ejecutar Vigía
    vigia = AgenteVigia(store, llm=NullLLM(), clock=lambda: now)
    solicitudes = vigia.revisar_casos_olvidados()
    assert len(solicitudes) > 0

    # Captura de tarjetas después de ejecutar vigía
    tarjetas_despues = incident_cards(store.since({}))
    niveles_despues = {c.incident_id: c.level for c in tarjetas_despues}
    conteo_eventos_despues = len(store.since({}))
    urgencias_despues = [
        (e.payload["report_id"], e.payload["level"], e.payload["floor"])
        for e in store.since({})
        if e.type == "urgencia_estimada"
    ]

    # El almacén de eventos es inmutable y no se agregaron eventos falsos de cambio de urgencia
    assert conteo_eventos_despues == conteo_eventos_antes
    # Niveles y pisos de urgencia son 100% idénticos
    assert niveles_despues == niveles_antes
    assert urgencias_despues == urgencias_antes
    assert niveles_despues["0000eeee-1"] == Urgency.CRITICA
    assert niveles_despues["0000ffff-1"] == Urgency.ALTA
    assert niveles_despues["00001111-1"] == Urgency.MEDIA


def test_vigia_con_scripted_llm_enriquece_motivos() -> None:
    """Con ScriptedLLM, usa la respuesta del LLM para el motivo."""
    store = InMemoryEventStore()
    now = 2000.0
    for ev in make_report("00002222-1", 1, now - 1000.0):
        store.append(ev)
    store.append(make_urgency("00002222-1", 3, Urgency.MEDIA, now - 1000.0))

    scripted = ScriptedLLM(
        [
            LLMReply(
                final={
                    "motivo": (
                        "Solicito a coordinacion asignar ficha de bomberos de inmediato por demora."
                    )
                }
            ),
        ]
    )
    vigia = AgenteVigia(store, llm=scripted, clock=lambda: now)
    solicitudes = vigia.revisar_casos_olvidados()

    coord_sols = [s for s in solicitudes if s.target_agent == "coordinacion"]
    assert len(coord_sols) == 1
    assert "asignar ficha de bomberos" in coord_sols[0].reason


def test_vigia_falla_llm_degrada_a_plantilla() -> None:
    """Si el LLM falla, el Vigía degrada elegantemente a la plantilla sin error."""
    store = InMemoryEventStore()
    now = 2000.0
    for ev in make_report("00003333-1", 1, now - 1000.0):
        store.append(ev)
    store.append(make_urgency("00003333-1", 3, Urgency.ALTA, now - 1000.0))

    scripted_vacio = ScriptedLLM([])  # Provoca LLMUnavailable
    vigia = AgenteVigia(store, llm=scripted_vacio, clock=lambda: now)
    solicitudes = vigia.revisar_casos_olvidados()

    assert len(solicitudes) >= 1
    assert all(len(s.reason) > 10 for s in solicitudes)
