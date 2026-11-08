"""Pruebas del Agente 6 de Coordinación (N10).

Verifica:
- La ficha publicada pasa validate_event y cumple el contrato.
- Aprobación humana: la ficha queda pendiente hasta que exista una decisión,
  y deja de estarlo después.
- Sin LLM (NullLLM) usa plantilla con from_template=True.
- Con ScriptedLLM que intenta proponer otra entidad, el responsable NO cambia (determinismo EMRE).
- Tipo "otro" (o sin responsable) -> responsible=None.
- Entidad sin unidades libres -> el mensaje lo advierte y sugiere escalar.
- Reprocesar el mismo incidente no duplica fichas.
- Un agente nuevo con el mismo store llega a las mismas conclusiones.
- Citas oficiales y citas de Retriever (RAG).
- Replanificación al recibir una decisión "rechazar".
"""

from __future__ import annotations

import itertools

from guadua.agents.coordinacion import (
    AgenteCoordinacion,
    coordinar_incidente,
    fichas_pendientes,
)
from guadua.knowledge.matriz_emre import responsable_por_tipo
from guadua.knowledge.retriever import Passage
from guadua.protocol.header import Incident, People
from guadua.providers.llm import LLMReply
from guadua.store.event_types import (
    Decision,
    Ficha,
    IncidenteAgrupado,
    Location,
    ReporteNormalizado,
    validate_event,
)
from guadua.store.events import Event, EventCategory
from tests.fakes import InMemoryEventStore, NullLLM, ScriptedLLM, StaticRetriever


class SeqGen:
    def __init__(self, start: int = 1) -> None:
        self._counter = itertools.count(start)

    def __call__(self) -> int:
        return next(self._counter)


def make_reporte_normalizado(
    report_id: str,
    incident: Incident,
    *,
    label: str = "El Cable",
    radius_m: int = 150,
    people: People = People.UNA,
    origin_node: str = "A",
    seq: int = 1,
    received_at: float = 1794573600.0,
) -> Event:
    payload = ReporteNormalizado(
        report_id=report_id,
        incident=incident,
        people=people,
        location=Location(geohash="d2fkcdc", radius_m=radius_m, label=label),
        direct_observation=True,
        vital_signs=(),
        risk_signals=(),
        text="reporte de prueba",
        unknown_fields=(),
    )
    ev = Event(
        origin_node=origin_node,
        seq=seq,
        category=EventCategory.DERIVADO,
        type="reporte_normalizado",
        payload=payload.model_dump(),
        refs=((origin_node, seq),),
        received_at=received_at,
    )
    validate_event(ev)
    return ev


def make_incidente_agrupado(
    incident_id: str,
    report_ids: tuple[str, ...],
    *,
    confidence: float = 0.8,
    origin_node: str = "A",
    seq: int = 2,
    received_at: float = 1794573600.0,
) -> Event:
    payload = IncidenteAgrupado(
        incident_id=incident_id,
        report_ids=report_ids,
        link="seguro",
        confidence=confidence,
        rumor=False,
        contradictions=(),
    )
    ev = Event(
        origin_node=origin_node,
        seq=seq,
        category=EventCategory.DERIVADO,
        type="incidente_agrupado",
        payload=payload.model_dump(),
        refs=((origin_node, 1),),
        received_at=received_at,
    )
    validate_event(ev)
    return ev


# --- Pruebas principales ----------------------------------------------------------------


def test_publica_ficha_valida_con_contrato() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen(10)

    # Reporte e incidente de colapso
    r = make_reporte_normalizado("a1b2c3d4-1", Incident.COLAPSO, seq=1)
    store.append(r)
    inc = make_incidente_agrupado("a1b2c3d4-1", ("a1b2c3d4-1",), seq=2)
    store.append(inc)

    agente = AgenteCoordinacion("A", seq_gen)
    ficha_ev = agente.procesar_incidente(inc, store)

    assert ficha_ev is not None
    assert ficha_ev.type == "ficha"
    assert ficha_ev.category == EventCategory.DERIVADO
    assert ficha_ev.refs == (inc.id,)

    # Validar cumplimiento del contrato de Ficha
    payload = validate_event(ficha_ev)
    assert isinstance(payload, Ficha)
    assert payload.incident_id == "a1b2c3d4-1"
    assert payload.responsible == "Cuerpo Oficial de Bomberos"
    assert payload.units_available is not None
    assert payload.units_available > 0
    assert payload.risk_warning is None  # D3b
    assert payload.from_template is True
    assert len(payload.citations) >= 1
    assert payload.citations[0].document == "EMRE Manizales"


def test_aprobacion_humana_fichas_pendientes() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen(10)
    agente = AgenteCoordinacion("A", seq_gen)

    r = make_reporte_normalizado("a1b2c3d4-1", Incident.INCENDIO, seq=1)
    store.append(r)
    inc = make_incidente_agrupado("a1b2c3d4-1", ("a1b2c3d4-1",), seq=2)
    store.append(inc)

    # 1. Al crearse, la ficha debe estar pendiente
    ficha_ev = agente.procesar_incidente(inc, store)
    assert ficha_ev is not None

    pendientes = fichas_pendientes(store)
    assert len(pendientes) == 1
    assert pendientes[0].id == ficha_ev.id

    # 2. El comandante toma una decisión de aprobar desde el tablero
    dec_payload = Decision(action="aprobar", operator="comandante-1")
    dec_ev = Event(
        origin_node="A",
        seq=seq_gen(),
        category=EventCategory.DECISION,
        type="decision",
        payload=dec_payload.model_dump(),
        refs=(ficha_ev.id,),
        received_at=1794573600.0,
    )
    validate_event(dec_ev)
    store.append(dec_ev)

    # 3. La ficha ya no debe estar pendiente
    pendientes_despues = fichas_pendientes(store)
    assert len(pendientes_despues) == 0


def test_sin_llm_usa_plantilla_con_from_template_true() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen(10)

    r = make_reporte_normalizado("a1b2c3d4-1", Incident.HERIDO, seq=1)
    store.append(r)
    inc = make_incidente_agrupado("a1b2c3d4-1", ("a1b2c3d4-1",), seq=2)
    store.append(inc)

    # Inyectar NullLLM (siempre falla)
    agente = AgenteCoordinacion("A", seq_gen, llm=NullLLM())
    ficha_ev = agente.procesar_incidente(inc, store)

    assert ficha_ev is not None
    payload = validate_event(ficha_ev)
    assert isinstance(payload, Ficha)
    assert payload.from_template is True
    assert "Atención de heridos en El Cable" in payload.message
    assert "Confianza: 80%" in payload.message


def test_llm_no_puede_cambiar_el_responsable_de_la_matriz() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen(10)

    r = make_reporte_normalizado("a1b2c3d4-1", Incident.INCENDIO, seq=1)
    store.append(r)
    inc = make_incidente_agrupado("a1b2c3d4-1", ("a1b2c3d4-1",), seq=2)
    store.append(inc)

    # ScriptedLLM que intenta proponer una entidad ficticia
    llm = ScriptedLLM(
        [
            LLMReply(
                final={
                    "message": "Incendio controlado por brigada vecinal.",
                    "responsible": "Entidad Falsa No Autorizada",
                }
            )
        ]
    )

    agente = AgenteCoordinacion("A", seq_gen, llm=llm)
    ficha_ev = agente.procesar_incidente(inc, store)

    assert ficha_ev is not None
    payload = validate_event(ficha_ev)
    assert isinstance(payload, Ficha)
    # El responsable DEBE ser Bomberos (según la EMRE), ignorando la sugerencia del LLM
    assert payload.responsible == "Cuerpo Oficial de Bomberos"
    assert payload.message == "Incendio controlado por brigada vecinal."
    assert payload.from_template is False


def test_tipo_otro_o_sin_responsable_resulta_none() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen(10)

    # Incidente tipo OTRO
    r = make_reporte_normalizado("a1b2c3d4-1", Incident.OTRO, seq=1)
    store.append(r)
    inc = make_incidente_agrupado("a1b2c3d4-1", ("a1b2c3d4-1",), seq=2)
    store.append(inc)

    agente = AgenteCoordinacion("A", seq_gen)
    ficha_ev = agente.procesar_incidente(inc, store)

    assert ficha_ev is not None
    payload = validate_event(ficha_ev)
    assert isinstance(payload, Ficha)
    assert payload.responsible is None
    assert payload.units_available is None
    assert "Sin responsable identificado" in payload.message
    assert "escalar al coordinador" in payload.message


def test_entidad_sin_unidades_libres_muestra_advertencia() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen(10)

    # Crear incidente donde la entidad no tiene unidades
    r = make_reporte_normalizado("a1b2c3d4-1", Incident.COLAPSO, seq=1)
    store.append(r)
    inc = make_incidente_agrupado("a1b2c3d4-1", ("a1b2c3d4-1",), seq=2)
    store.append(inc)

    # Usar agente que consulte recursos simulados con 0 unidades
    agente = AgenteCoordinacion("A", seq_gen)

    # Sobrescribir temporalmente la función recursos_disponibles para devolver 0
    import guadua.agents.coordinacion as coord_mod

    orig_recursos = coord_mod.recursos_disponibles
    coord_mod.recursos_disponibles = lambda ent: 0
    try:
        ficha_ev = agente.procesar_incidente(inc, store)
        assert ficha_ev is not None
        payload = validate_event(ficha_ev)
        assert isinstance(payload, Ficha)
        assert payload.units_available == 0
        assert "no tiene unidades disponibles" in payload.message
        assert "escalar al coordinador" in payload.message
    finally:
        coord_mod.recursos_disponibles = orig_recursos


def test_idempotencia_no_duplica_fichas_para_mismo_incidente() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen(10)
    agente = AgenteCoordinacion("A", seq_gen)

    r = make_reporte_normalizado("a1b2c3d4-1", Incident.INCENDIO, seq=1)
    store.append(r)
    inc = make_incidente_agrupado("a1b2c3d4-1", ("a1b2c3d4-1",), seq=2)
    store.append(inc)

    ev1 = agente.procesar_incidente(inc, store)
    assert ev1 is not None

    # Reprocesar el mismo incidente no genera un nuevo evento
    ev2 = agente.procesar_incidente(inc, store)
    assert ev2 is None

    # Solo existe 1 ficha en el store
    fichas = [e for e in store.since({}) if e.type == "ficha"]
    assert len(fichas) == 1


def test_reconstruccion_desde_store_mismas_conclusiones() -> None:
    store = InMemoryEventStore()
    agente1 = AgenteCoordinacion("A", SeqGen(10))

    r = make_reporte_normalizado("a1b2c3d4-1", Incident.FUGA_GAS, seq=1)
    store.append(r)
    inc = make_incidente_agrupado("a1b2c3d4-1", ("a1b2c3d4-1",), seq=2)
    store.append(inc)

    agente1.procesar_incidente(inc, store)

    # Nuevo agente sin memoria previa
    agente2 = AgenteCoordinacion("B", SeqGen(100))

    # Debe detectar que la ficha ya existe y no duplicar
    assert agente2.procesar_incidente(inc, store) is None

    # Fichas pendientes deben reflejar la ficha creada
    pendientes = fichas_pendientes(store)
    assert len(pendientes) == 1
    assert pendientes[0].payload["responsible"] == "Cuerpo Oficial de Bomberos"


def test_replanificacion_al_rechazar_ficha() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen(10)
    agente = AgenteCoordinacion("A", seq_gen)

    # Incidente con ATRAPADOS (L: Bomberos, R: Defensa Civil, Cruz Roja)
    r = make_reporte_normalizado("a1b2c3d4-1", Incident.ATRAPADOS, seq=1)
    store.append(r)
    inc = make_incidente_agrupado("a1b2c3d4-1", ("a1b2c3d4-1",), seq=2)
    store.append(inc)

    # 1. Primera ficha asigna a Bomberos
    ficha1 = agente.procesar_incidente(inc, store)
    assert ficha1 is not None
    assert ficha1.payload["responsible"] == "Cuerpo Oficial de Bomberos"

    # 2. El operador rechaza la ficha
    dec_rechazo = Event(
        origin_node="A",
        seq=seq_gen(),
        category=EventCategory.DECISION,
        type="decision",
        payload=Decision(action="rechazar", operator="comandante-1").model_dump(),
        refs=(ficha1.id,),
        received_at=1794573600.0,
    )
    validate_event(dec_rechazo)
    store.append(dec_rechazo)

    # 3. Procesar la decisión para replanear
    ficha2 = agente.procesar_decision(dec_rechazo, store)
    assert ficha2 is not None
    p2 = validate_event(ficha2)
    assert isinstance(p2, Ficha)
    # Se debe asignar la siguiente entidad responsable de la matriz (Defensa Civil Colombiana)
    assert p2.responsible == "Defensa Civil Colombiana"
    assert "Replanificación" in p2.message


def test_integracion_con_retriever_agrega_cita() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen(10)

    # Crear retriever estático simulado
    pasaje = Passage(
        document="Protocolo Rescate Urbano",
        section="3.1",
        version="2024",
        text="Procedimiento de extracción en colapso",
        score=0.95,
        is_example=False,
    )
    retriever = StaticRetriever([pasaje])

    r = make_reporte_normalizado("a1b2c3d4-1", Incident.COLAPSO, seq=1)
    store.append(r)
    inc = make_incidente_agrupado("a1b2c3d4-1", ("a1b2c3d4-1",), seq=2)
    store.append(inc)

    agente = AgenteCoordinacion("A", seq_gen, retriever=retriever)
    ficha_ev = agente.procesar_incidente(inc, store)

    assert ficha_ev is not None
    p = validate_event(ficha_ev)
    assert isinstance(p, Ficha)
    # Debe tener al menos dos citas: la de la EMRE y la del pasaje RAG
    docs_citados = [c.document for c in p.citations]
    assert "EMRE Manizales" in docs_citados
    assert "Protocolo Rescate Urbano" in docs_citados
    assert any(c.section == "3.1" for c in p.citations)


def test_convenience_function_coordinar_incidente() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen(10)

    r = make_reporte_normalizado("a1b2c3d4-1", Incident.INCENDIO, seq=1)
    store.append(r)
    inc = make_incidente_agrupado("a1b2c3d4-1", ("a1b2c3d4-1",), seq=2)
    store.append(inc)

    ficha_ev = coordinar_incidente(inc, store, "A", seq_gen)
    assert ficha_ev is not None
    assert ficha_ev.type == "ficha"
    assert ficha_ev.payload["responsible"] == "Cuerpo Oficial de Bomberos"


def test_matriz_emre_funciones_puras() -> None:
    # Probar que responsable_por_tipo funciona para todos los tipos
    assert responsable_por_tipo(Incident.ATRAPADOS) == "Cuerpo Oficial de Bomberos"
    assert responsable_por_tipo(Incident.COLAPSO) == "Cuerpo Oficial de Bomberos"
    assert responsable_por_tipo(Incident.INCENDIO) == "Cuerpo Oficial de Bomberos"
    assert responsable_por_tipo(Incident.FUGA_GAS) == "Cuerpo Oficial de Bomberos"
    assert responsable_por_tipo(Incident.HERIDO) == "Secretaría de Salud"
    assert responsable_por_tipo(Incident.VIA_BLOQUEADA) == "Secretaría de Infraestructura"
    assert responsable_por_tipo(Incident.OTRO) is None

    # Exclusión para replanificación
    alt = responsable_por_tipo(Incident.ATRAPADOS, excluidos={"Cuerpo Oficial de Bomberos"})
    assert alt == "Defensa Civil Colombiana"
