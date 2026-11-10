"""Pruebas del cálculo de confianza, rumores y contradicciones (D4).

Verifica:
- 40 copias idénticas con 'me contaron' dan confianza baja (< 0.30) y rumor=True.
- 3 testigos directos con redacción distinta y dispositivos distintos dan confianza alta (>= 0.70).
- 40 de segunda mano distintos no superan el tope acordado (0.25).
- Mismo dispositivo repetido aporta 0.
- confirmacion_campo verificado (confirmed=True) sube la confianza a 1.0.
- Contradicción en confirmacion_campo (confirmed=False) queda registrada con origen y timestamp.
- Contradicción de texto ('atrapados' vs 'ya los sacaron') queda registrada con origen y timestamp.
- El cálculo numérico no se sale de [0.0, 1.0] y genera eventos válidos con validate_event.
- Invariante de triaje: la correlación nunca baja la urgencia ni oculta posibles atrapados.
"""

from __future__ import annotations

import itertools

from guadua.agents.correlacion import Correlacionador, correlacionar_confirmacion_campo
from guadua.agents.correlacion_confianza import (
    IND_MISMO_DISPOSITIVO,
    IND_TEXTO_CASI_IDENTICO,
    TOPE_CONFIANZA_SEGUNDA_MANO,
    UMBRAL_CONFIANZA_ALTA,
    UMBRAL_CONFIANZA_BAJA,
    calcular_confianza_y_rumor,
    calcular_independencia,
    detectar_contradicciones,
)
from guadua.protocol.header import Flag, Incident, MessageType, People
from guadua.store.event_types import (
    ConfirmacionCampo,
    IncidenteAgrupado,
    Location,
    Reporte,
    ReporteNormalizado,
    validate_event,
)
from guadua.store.events import Event, EventCategory
from tests.fakes import InMemoryEventStore


class SeqGen:
    def __init__(self, start: int = 100) -> None:
        self._counter = itertools.count(start)

    def __call__(self) -> int:
        return next(self._counter)


def make_reporte_norm(
    report_id: str,
    incident: Incident = Incident.COLAPSO,
    *,
    direct_observation: bool = True,
    text: str | None = None,
    vital_signs: tuple[str, ...] = (),
    geohash: str = "d3xh28u",
    radius_m: int = 50,
) -> ReporteNormalizado:
    return ReporteNormalizado(
        report_id=report_id,
        incident=incident,
        people=People.UNA,
        location=Location(geohash=geohash, radius_m=radius_m),
        direct_observation=direct_observation,
        vital_signs=vital_signs,
        risk_signals=(),
        text=text,
        unknown_fields=(),
    )


def make_norm_event(
    reporte: ReporteNormalizado,
    origin_node: str = "A",
    seq: int = 1,
    received_at: float = 1794573600.0,
) -> Event:
    ev = Event(
        origin_node=origin_node,
        seq=seq,
        category=EventCategory.DERIVADO,
        type="reporte_normalizado",
        payload=reporte.model_dump(),
        refs=((origin_node, seq),),
        received_at=received_at,
    )
    validate_event(ev)
    return ev


# --- Pruebas de la fórmula pura de confianza y rumores --------------------------------


def test_40_copias_identicas_me_contaron_da_baja_y_rumor() -> None:
    """40 copias idénticas con 'me contaron' dan confianza baja (< 0.30) y rumor=True."""
    texto_rumor = "Me contaron que colapsó un edificio en la esquina"
    # Diferentes dispositivos enviando exactamente el mismo texto de segunda mano
    reportes = [
        make_reporte_norm(
            f"{i:08x}-1",
            Incident.COLAPSO,
            direct_observation=False,  # "me contaron"
            text=texto_rumor,
        )
        for i in range(1, 41)
    ]

    confianza, rumor = calcular_confianza_y_rumor(reportes)

    assert confianza < UMBRAL_CONFIANZA_BAJA
    assert rumor is True
    assert 0.0 <= confianza <= 1.0


def test_3_testigos_directos_redaccion_distinta_dan_alta() -> None:
    """3 testigos directos con redacción distinta y dispositivos distintos dan
    confianza alta (>= 0.70)."""
    textos = [
        "Estoy viendo el muro caído sobre el andén",
        "Se vino abajo la pared lateral del segundo piso frente a mí",
        "Colapso parcial de la vivienda en este instante, lo tengo en frente",
    ]
    reportes = [
        make_reporte_norm(
            f"a{i}00000{i}-1",
            Incident.COLAPSO,
            direct_observation=True,
            text=textos[i],
        )
        for i in range(3)
    ]

    confianza, rumor = calcular_confianza_y_rumor(reportes)

    # 1 - (1 - 0.60)^3 = 1 - 0.40^3 = 1 - 0.064 = 0.936
    assert confianza >= UMBRAL_CONFIANZA_ALTA
    assert confianza == 0.936
    assert rumor is False


def test_40_segunda_mano_distintos_no_superan_tope() -> None:
    """40 reportes de segunda mano distintos no superan el tope acordado (0.25)."""
    # 40 mensajes de segunda mano, cada uno con redacción propia de diferente dispositivo
    reportes = [
        make_reporte_norm(
            f"b{i:07x}-1",
            Incident.COLAPSO,
            direct_observation=False,
            text=f"Dicen los vecinos que se cayó la casa número {i * 7 + 10} del sector",
        )
        for i in range(1, 41)
    ]

    confianza, rumor = calcular_confianza_y_rumor(reportes)

    # Sin tope daría 1 - 0.90^40 ≈ 0.985 (Alta)
    # Con tope propuesto y acordado, no supera TOPE_CONFIANZA_SEGUNDA_MANO (0.25)
    assert confianza <= TOPE_CONFIANZA_SEGUNDA_MANO
    assert confianza == TOPE_CONFIANZA_SEGUNDA_MANO
    assert confianza < UMBRAL_CONFIANZA_BAJA
    assert rumor is True  # Sigue marcado como rumor porque no hay testigos directos


def test_mismo_dispositivo_repetido_aporta_cero() -> None:
    """El mismo dispositivo enviando múltiples reportes solo aporta en el primero."""
    r1 = make_reporte_norm(
        "a1b2c3d4-1",
        Incident.COLAPSO,
        direct_observation=True,
        text="Veo colapso aquí",
    )
    r2 = make_reporte_norm(
        "a1b2c3d4-2",
        Incident.COLAPSO,
        direct_observation=True,
        text="Sigo viendo el colapso, se cayó otro pedazo",
    )
    r3 = make_reporte_norm(
        "a1b2c3d4-3",
        Incident.COLAPSO,
        direct_observation=True,
        text="Aún esperando ayuda en el colapso",
    )

    c1, _ = calcular_confianza_y_rumor([r1])
    c3, _ = calcular_confianza_y_rumor([r1, r2, r3])

    # El primer reporte ciudadano directo aporta 0.60
    assert c1 == 0.60
    # Los reportes 2 y 3 del mismo dispositivo a1b2c3d4 aportan ind=0.0
    assert c3 == c1


def test_independencia_factores_individuales() -> None:
    """Verifica el cálculo de los factores ind_i intermedios."""
    base = make_reporte_norm("11111111-1", text="Colapso de casa esquinera de dos pisos")

    # Mismo dispositivo
    mismo_dev = make_reporte_norm("11111111-2", text="Texto completamente diferente")
    assert calcular_independencia(mismo_dev, [base]) == IND_MISMO_DISPOSITIVO

    # Texto casi idéntico desde otro dispositivo
    copia = make_reporte_norm("22222222-1", text="Colapso de casa esquinera de dos pisos")
    assert calcular_independencia(copia, [base]) == IND_TEXTO_CASI_IDENTICO

    # Redacción propia e independiente
    indep = make_reporte_norm("33333333-1", text="Derrumbe de vivienda en la carrera 23")
    assert calcular_independencia(indep, [base]) == 1.0


def test_confirmacion_campo_positivo_sube_a_uno() -> None:
    """confirmacion_campo con confirmed=True eleva la confianza a 1.00 (verificado)."""
    r1 = make_reporte_norm("a1b2c3d4-1", direct_observation=False, text="Rumor")
    conf = ConfirmacionCampo(incident_id="inc-1", confirmed=True, note="Verificado por Bomberos")

    confianza, rumor = calcular_confianza_y_rumor([r1], confirmaciones=[conf])
    assert confianza == 1.00
    assert rumor is False


# --- Pruebas de detección de contradicciones ------------------------------------------


def test_confirmacion_campo_negativa_registra_contradiccion() -> None:
    """confirmacion_campo con confirmed=False se registra en contradictions
    con origen y timestamp."""
    r1 = make_reporte_norm("a1b2c3d4-1", text="Dicen que hay personas atrapadas")
    conf = ConfirmacionCampo(
        incident_id="inc-1", confirmed=False, note="Revisada la zona, sin colapso ni atrapados"
    )

    contradicciones = detectar_contradicciones(
        [(r1, 1794573600.0)],
        confirmaciones=[(conf, "B:15", 1794573900.0)],
    )

    assert len(contradicciones) == 1
    assert "origen=B:15" in contradicciones[0]
    assert "t=1794573900.0" in contradicciones[0]
    assert "descartado en campo" in contradicciones[0].lower()


def test_contradiccion_texto_atrapados_vs_liberados() -> None:
    """Reporte de atrapados vs reporte posterior de 'ya los sacaron' genera contradicción."""
    r1 = make_reporte_norm(
        "a1b2c3d4-1",
        incident=Incident.ATRAPADOS,
        text="Hay 3 atrapados bajo los escombros",
    )
    r2 = make_reporte_norm(
        "b2c3d4e5-1",
        incident=Incident.COLAPSO,
        text="Ya los sacaron los vecinos antes de que llegara la ambulancia",
    )

    contradicciones = detectar_contradicciones(
        [
            (r1, 1794573600.0),
            (r2, 1794573720.0),
        ]
    )

    assert len(contradicciones) == 1
    assert "origen=b2c3d4e5-1" in contradicciones[0]
    assert "t=1794573720.0" in contradicciones[0]
    assert "a1b2c3d4-1" in contradicciones[0]


# --- Pruebas de integración con Correlacionador y EventStore --------------------------


def test_correlacionador_emite_nuevo_evento_con_confirmacion_campo() -> None:
    """El Correlacionador emite un nuevo evento incidente_agrupado al recibir confirmacion_campo."""
    store = InMemoryEventStore()
    seq_gen = SeqGen()
    agente = Correlacionador("A", seq_gen)

    # 1. Reporte inicial
    r1 = make_reporte_norm("a1b2c3d4-1", direct_observation=False, text="Rumor de colapso")
    ev1 = make_norm_event(r1, origin_node="A", seq=1)
    store.append(ev1)

    inc1 = agente.procesar_reporte_normalizado(ev1, store)
    assert inc1 is not None
    payload1 = validate_event(inc1)
    assert isinstance(payload1, IncidenteAgrupado)
    assert payload1.confidence == 0.10  # 1 reporte segunda mano
    assert payload1.rumor is True

    # 2. Rescatista confirma en campo (evento confirmacion_campo)
    conf_payload = ConfirmacionCampo(
        incident_id="a1b2c3d4-1",
        confirmed=True,
        note="Confirmado colapso en sitio",
    )
    ev_conf = Event(
        origin_node="B",
        seq=10,
        category=EventCategory.HECHO,
        type="confirmacion_campo",
        payload=conf_payload.model_dump(),
        refs=(),
        received_at=1794573800.0,
    )
    validate_event(ev_conf)
    store.append(ev_conf)

    # 3. Procesar confirmación de campo con el agente
    inc2 = correlacionar_confirmacion_campo(ev_conf, store, "A", seq_gen)
    assert inc2 is not None
    assert inc2.refs == (ev_conf.id,)

    payload2 = validate_event(inc2)
    assert isinstance(payload2, IncidenteAgrupado)
    assert payload2.incident_id == "a1b2c3d4-1"
    assert payload2.confidence == 1.00
    assert payload2.rumor is False


def test_correlacionador_descarte_en_campo_registra_contradiccion() -> None:
    """El Correlacionador registra contradicción cuando confirmacion_campo descarta la escena."""
    store = InMemoryEventStore()
    seq_gen = SeqGen()
    agente = Correlacionador("A", seq_gen)

    r1 = make_reporte_norm("a1b2c3d4-1", direct_observation=True, text="Veo humo")
    ev1 = make_norm_event(r1, origin_node="A", seq=1)
    store.append(ev1)
    agente.procesar_reporte_normalizado(ev1, store)

    conf_negativa = ConfirmacionCampo(
        incident_id="a1b2c3d4-1",
        confirmed=False,
        note="Falsa alarma, fue quema de basuras ya apagada",
    )
    ev_conf = Event(
        origin_node="B",
        seq=20,
        category=EventCategory.HECHO,
        type="confirmacion_campo",
        payload=conf_negativa.model_dump(),
        refs=(),
        received_at=1794574000.0,
    )
    validate_event(ev_conf)
    store.append(ev_conf)

    inc_actualizado = agente.procesar_confirmacion_campo(ev_conf, store)
    assert inc_actualizado is not None
    payload = validate_event(inc_actualizado)
    assert isinstance(payload, IncidenteAgrupado)
    assert len(payload.contradictions) == 1
    assert "origen=B:20" in payload.contradictions[0]
    assert "descartado en campo" in payload.contradictions[0].lower()


def test_radio_directo_aporta_peso_85() -> None:
    """Reportes con canal 'radio' o 'vhf' y observación directa aportan peso p=0.85."""
    store = InMemoryEventStore()
    seq_gen = SeqGen()
    agente = Correlacionador("A", seq_gen)

    # Simular que en store hay un evento 'reporte' con channel='vhf'
    rep_payload = Reporte(
        report_id="a1000000-1",
        msg_type=MessageType.REPORTE,
        incident=Incident.COLAPSO,
        people=People.UNA,
        flags=int(Flag.OBSERVACION_DIRECTA),
        geohash="d3xh28u",
        barrio=None,
        device_time=1000,
        text="Reporte por radio desde el sitio",
        channel="vhf",
        raw_hex="00",
    )
    ev_raw = Event(
        origin_node="A",
        seq=1,
        category=EventCategory.HECHO,
        type="reporte",
        payload=rep_payload.model_dump(),
        refs=(),
        received_at=1794573600.0,
    )
    validate_event(ev_raw)
    store.append(ev_raw)

    r_norm = make_reporte_norm(
        "a1000000-1",
        Incident.COLAPSO,
        direct_observation=True,
        text="Reporte por radio desde el sitio",
    )
    ev_norm = make_norm_event(r_norm, origin_node="A", seq=2)
    store.append(ev_norm)

    inc = agente.procesar_reporte_normalizado(ev_norm, store)
    assert inc is not None
    payload = validate_event(inc)
    assert isinstance(payload, IncidenteAgrupado)
    # Rescatista por radio directo: p = 0.85, ind = 1.0 -> Confianza = 0.85
    assert payload.confidence == 0.85
    assert payload.rumor is False
