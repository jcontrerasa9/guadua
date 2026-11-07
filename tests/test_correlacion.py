"""Pruebas del Agente 3 de Correlación (N9).

Verifica:
- Publicación de `incidente_agrupado` válido (validate_event).
- Vinculación de reportes duplicados/cercanos como "seguro".
- Prevención del efecto cadena (A~B, B~C, A~C) comparando contra el centro.
- Casos limítrofes como "dudoso".
- Reconstrucción de estado desde el store e idempotencia (no duplicar report_id).
- Criterios de compatibilidad de tipos, ventanas de tiempo y ubicación (barrio / geohash).
"""

from __future__ import annotations

import itertools

import pytest

from guadua.agents.correlacion import (
    CONFIDENCE_DUDOSO,
    CONFIDENCE_NUEVO,
    CONFIDENCE_SEGURO,
    Correlacionador,
    correlacionar_reporte,
    haversine_distance_m,
)
from guadua.protocol.header import GEOHASH_ALPHABET, Incident, People
from guadua.store.event_types import (
    IncidenteAgrupado,
    Location,
    ReporteNormalizado,
    validate_event,
)
from guadua.store.events import Event, EventCategory
from tests.fakes import InMemoryEventStore


def encode_geohash(lat: float, lon: float, precision: int = 7) -> str:
    """Codifica coordenadas en un geohash para pruebas."""
    lat_interval = [-90.0, 90.0]
    lon_interval = [-180.0, 180.0]
    geohash: list[str] = []
    is_even = True
    bit = 0
    ch = 0
    while len(geohash) < precision:
        if is_even:
            mid = (lon_interval[0] + lon_interval[1]) / 2.0
            if lon >= mid:
                ch |= 1 << (4 - bit)
                lon_interval[0] = mid
            else:
                lon_interval[1] = mid
        else:
            mid = (lat_interval[0] + lat_interval[1]) / 2.0
            if lat >= mid:
                ch |= 1 << (4 - bit)
                lat_interval[0] = mid
            else:
                lat_interval[1] = mid
        is_even = not is_even
        if bit < 4:
            bit += 1
        else:
            geohash.append(GEOHASH_ALPHABET[ch])
            bit = 0
            ch = 0
    return "".join(geohash)


def make_reporte_normalizado_event(
    report_id: str,
    incident: Incident,
    *,
    geohash: str | None = None,
    barrio: int | None = None,
    radius_m: int = 50,
    text: str | None = None,
    received_at: float = 1794573600.0,
    origin_node: str = "A",
    seq: int = 1,
) -> Event:
    """Crea un Event de tipo reporte_normalizado válido."""
    location = Location(
        geohash=geohash,
        barrio=barrio,
        radius_m=radius_m,
        label="Test Location",
    )
    payload = ReporteNormalizado(
        report_id=report_id,
        incident=incident,
        people=People.UNA,
        location=location,
        direct_observation=True,
        vital_signs=(),
        risk_signals=(),
        text=text,
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


class SeqGen:
    def __init__(self, start: int = 100) -> None:
        self._counter = itertools.count(start)

    def __call__(self) -> int:
        return next(self._counter)


# --- Pruebas básicas y contrato --------------------------------------------------------


def test_publica_incidente_agrupado_valido() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen()
    agente = Correlacionador("A", seq_gen)

    r1 = make_reporte_normalizado_event(
        "a1b2c3d4-1",
        Incident.COLAPSO,
        geohash=encode_geohash(5.0695, -75.5180),
        radius_m=60,
        text="colapso de casa",
    )
    store.append(r1)

    evento_inc = agente.procesar_reporte_normalizado(r1, store)
    assert evento_inc is not None
    assert evento_inc.type == "incidente_agrupado"
    assert evento_inc.category == EventCategory.DERIVADO
    assert evento_inc.refs == (r1.id,)
    assert evento_inc.received_at == r1.received_at

    # Validar con validate_event (contrato)
    payload = validate_event(evento_inc)
    assert isinstance(payload, IncidenteAgrupado)
    assert payload.incident_id == "a1b2c3d4-1"
    assert payload.report_ids == ("a1b2c3d4-1",)
    assert payload.link == "nuevo"
    assert payload.confidence == CONFIDENCE_NUEVO


def test_vincula_duplicados_como_seguro() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen()
    agente = Correlacionador("A", seq_gen)

    # Reporte 1: colapso en punto X
    gh = encode_geohash(5.0695, -75.5180)
    r1 = make_reporte_normalizado_event("a1b2c3d4-1", Incident.COLAPSO, geohash=gh, radius_m=80)
    store.append(r1)
    ev1 = agente.procesar_reporte_normalizado(r1, store)
    assert ev1 is not None

    # Reporte 2: reporte cercano con tipo altamente compatible (atrapados)
    r2 = make_reporte_normalizado_event(
        "a1b2c3d4-2",
        Incident.ATRAPADOS,
        geohash=gh,
        radius_m=80,
        received_at=r1.received_at + 600.0,
    )
    store.append(r2)
    ev2 = agente.procesar_reporte_normalizado(r2, store)
    assert ev2 is not None

    payload = validate_event(ev2)
    assert isinstance(payload, IncidenteAgrupado)
    assert payload.incident_id == "a1b2c3d4-1"  # Conserva el ID del primer reporte
    assert payload.report_ids == ("a1b2c3d4-1", "a1b2c3d4-2")
    assert payload.link == "seguro"
    assert payload.confidence == CONFIDENCE_SEGURO


# --- Prueba de No Efecto Cadena (anti-chaining) ----------------------------------------


def test_sin_efecto_cadena() -> None:
    """Verifica que A~B y B~C no causa A~C cuando C se compara contra el centro de A+B.

    Configuración:
    - Reporte A en lat 5.069000, radio 50 m.
    - Reporte B a 80 m de A hacia el norte (~5.069723). Radio 50 m.
      Distancia A a B = 80 m <= 50+50 m -> Se unen en grupo seguro.
      Centroide resultante está a 40 m de A (~5.069361), con radio máx = 50 m.
    - Reporte C a 160 m de A hacia el norte (80 m de B, ~5.070446). Radio 50 m.
      Distancia de C a B es 80 m (se uniría si fuera contra B individual).
      Distancia de C al CENTROIDE del grupo es 120 m.
      R_total = 50 + 50 = 100 m.
      Distancia 120 m > R_total (100 m) -> NO se une como seguro.
    - Reporte D a 320 m de A hacia el norte (160 m de C).
      Distancia al centroide = 280 m > 100 + 150 = 250 m -> Es "nuevo".
    """
    store = InMemoryEventStore()
    seq_gen = SeqGen()
    agente = Correlacionador("A", seq_gen)

    lon = -75.5180
    lat_a = 5.069000
    lat_b = 5.069723  # ~80 m norte
    lat_c = 5.070446  # ~160 m norte de A (~80 m norte de B)
    lat_d = 5.071890  # ~320 m norte de A

    # Verificar distancias reales Haversine
    dist_ab = haversine_distance_m(lat_a, lon, lat_b, lon)
    dist_bc = haversine_distance_m(lat_b, lon, lat_c, lon)
    assert 75.0 <= dist_ab <= 85.0
    assert 75.0 <= dist_bc <= 85.0

    gh_a = encode_geohash(lat_a, lon)
    gh_b = encode_geohash(lat_b, lon)
    gh_c = encode_geohash(lat_c, lon)
    gh_d = encode_geohash(lat_d, lon)

    # 1. Reporte A entra como nuevo
    r_a = make_reporte_normalizado_event("0000000a-1", Incident.COLAPSO, geohash=gh_a, radius_m=50)
    store.append(r_a)
    ev_a = agente.procesar_reporte_normalizado(r_a, store)
    assert ev_a is not None
    assert validate_event(ev_a).link == "nuevo"

    # 2. Reporte B se une con A como seguro
    r_b = make_reporte_normalizado_event("0000000b-1", Incident.COLAPSO, geohash=gh_b, radius_m=50)
    store.append(r_b)
    ev_b = agente.procesar_reporte_normalizado(r_b, store)
    assert ev_b is not None
    p_b = validate_event(ev_b)
    assert p_b.link == "seguro"
    assert p_b.report_ids == ("0000000a-1", "0000000b-1")

    # 3. Reporte C: está cerca de B (80 m), pero lejos del centroide de A+B (120 m > 100 m)
    r_c = make_reporte_normalizado_event("0000000c-1", Incident.COLAPSO, geohash=gh_c, radius_m=50)
    store.append(r_c)
    ev_c = agente.procesar_reporte_normalizado(r_c, store)
    assert ev_c is not None
    p_c = validate_event(ev_c)
    # No se unió al grupo A con link "seguro"
    assert p_c.link != "seguro"
    assert "0000000a-1" not in p_c.report_ids

    # 4. Reporte D: aún más lejos, completamente "nuevo"
    r_d = make_reporte_normalizado_event("0000000d-1", Incident.COLAPSO, geohash=gh_d, radius_m=50)
    store.append(r_d)
    ev_d = agente.procesar_reporte_normalizado(r_d, store)
    assert ev_d is not None
    p_d = validate_event(ev_d)
    assert p_d.link == "nuevo"
    assert p_d.incident_id == "0000000d-1"


# --- Pruebas de Reconstrucción de Estado e Idempotencia ---------------------------------


def test_idempotencia_no_duplica_report_id() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen()
    agente = Correlacionador("A", seq_gen)

    gh = encode_geohash(5.0695, -75.5180)
    r1 = make_reporte_normalizado_event("00000001-1", Incident.INCENDIO, geohash=gh, radius_m=100)
    store.append(r1)

    ev1 = agente.procesar_reporte_normalizado(r1, store)
    assert ev1 is not None

    # Reprocesar el mismo reporte
    ev1_reintentado = agente.procesar_reporte_normalizado(r1, store)
    assert ev1_reintentado is None  # No publica duplicados

    # El store contiene únicamente 1 incidente_agrupado
    incident_events = [e for e in store.since({}) if e.type == "incidente_agrupado"]
    assert len(incident_events) == 1


def test_reconstruccion_desde_store_mismas_conclusiones() -> None:
    store = InMemoryEventStore()
    gh = encode_geohash(5.0695, -75.5180)

    # Agente 1 procesa dos reportes
    agente1 = Correlacionador("A", SeqGen(10))
    r1 = make_reporte_normalizado_event(
        "00000001-1", Incident.FUGA_GAS, geohash=gh, radius_m=60, seq=1
    )
    store.append(r1)
    agente1.procesar_reporte_normalizado(r1, store)

    r2 = make_reporte_normalizado_event(
        "00000001-2", Incident.INCENDIO, geohash=gh, radius_m=60, seq=2
    )
    store.append(r2)
    agente1.procesar_reporte_normalizado(r2, store)

    # Crear una instancia completamente nueva de Correlacionador sin estado previo
    agente2 = Correlacionador("B", SeqGen(100))

    # El agente 2 debe reconocer que rep-1 y rep-2 ya están agrupados
    assert agente2.procesar_reporte_normalizado(r1, store) is None
    assert agente2.procesar_reporte_normalizado(r2, store) is None

    # Y un tercer reporte que llega debe vincularse al grupo existente reconstruido del store
    r3 = make_reporte_normalizado_event(
        "00000001-3", Incident.INCENDIO, geohash=gh, radius_m=60, seq=3
    )
    store.append(r3)
    ev3 = agente2.procesar_reporte_normalizado(r3, store)
    assert ev3 is not None
    p3 = validate_event(ev3)
    assert p3.incident_id == "00000001-1"
    assert p3.report_ids == ("00000001-1", "00000001-2", "00000001-3")
    assert p3.link == "seguro"


# --- Criterios de Ubicación: Barrio vs Geohash -----------------------------------------


def test_mismo_barrio_con_alta_compatibilidad_y_texto() -> None:
    store = InMemoryEventStore()
    agente = Correlacionador("A", SeqGen())

    # Reporte 1 en Barrio 10
    r1 = make_reporte_normalizado_event(
        "00000002-1",
        Incident.COLAPSO,
        barrio=10,
        text="colapso estructural en vivienda de dos pisos",
    )
    store.append(r1)
    agente.procesar_reporte_normalizado(r1, store)

    # Reporte 2 en Barrio 10 con alta compatibilidad y texto similar -> seguro
    r2 = make_reporte_normalizado_event(
        "00000002-2",
        Incident.ATRAPADOS,
        barrio=10,
        text="colapso de vivienda dos pisos personas atrapadas",
    )
    store.append(r2)
    ev2 = agente.procesar_reporte_normalizado(r2, store)
    assert ev2 is not None
    p2 = validate_event(ev2)
    assert p2.link == "seguro"
    assert p2.incident_id == "00000002-1"


def test_mismo_barrio_sin_texto_o_texto_disimil_queda_dudoso() -> None:
    store = InMemoryEventStore()
    agente = Correlacionador("A", SeqGen())

    r1 = make_reporte_normalizado_event("00000002-1", Incident.COLAPSO, barrio=10, text=None)
    store.append(r1)
    agente.procesar_reporte_normalizado(r1, store)

    # Sin texto suficiente para confirmar, queda máximo "dudoso"
    r2 = make_reporte_normalizado_event("00000002-2", Incident.COLAPSO, barrio=10, text=None)
    store.append(r2)
    ev2 = agente.procesar_reporte_normalizado(r2, store)
    assert ev2 is not None
    p2 = validate_event(ev2)
    assert p2.link == "dudoso"
    assert p2.incident_id == "00000002-2"


def test_barrios_distintos_resulta_nuevo() -> None:
    store = InMemoryEventStore()
    agente = Correlacionador("A", SeqGen())

    r1 = make_reporte_normalizado_event("00000002-1", Incident.INCENDIO, barrio=10)
    store.append(r1)
    agente.procesar_reporte_normalizado(r1, store)

    r2 = make_reporte_normalizado_event("00000002-2", Incident.INCENDIO, barrio=12)
    store.append(r2)
    ev2 = agente.procesar_reporte_normalizado(r2, store)
    assert ev2 is not None
    assert validate_event(ev2).link == "nuevo"


def test_barrio_contra_geohash_resulta_dudoso() -> None:
    store = InMemoryEventStore()
    agente = Correlacionador("A", SeqGen())

    r1 = make_reporte_normalizado_event(
        "00000002-1", Incident.INCENDIO, geohash=encode_geohash(5.0695, -75.5180)
    )
    store.append(r1)
    agente.procesar_reporte_normalizado(r1, store)

    r2 = make_reporte_normalizado_event("00000002-2", Incident.INCENDIO, barrio=10)
    store.append(r2)
    ev2 = agente.procesar_reporte_normalizado(r2, store)
    assert ev2 is not None
    p2 = validate_event(ev2)
    assert p2.link == "dudoso"
    assert p2.confidence == CONFIDENCE_DUDOSO
    assert p2.incident_id == "00000002-2"


# --- Ventanas de Tiempo -----------------------------------------------------------------


def test_ventana_de_tiempo_superada_resulta_nuevo() -> None:
    store = InMemoryEventStore()
    agente = Correlacionador("A", SeqGen())
    gh = encode_geohash(5.0695, -75.5180)

    t0 = 1794573600.0
    r1 = make_reporte_normalizado_event("00000003-1", Incident.COLAPSO, geohash=gh, received_at=t0)
    store.append(r1)
    agente.procesar_reporte_normalizado(r1, store)

    # 3 horas después (> TIME_WINDOW_SECONDS de 2 horas)
    r2 = make_reporte_normalizado_event(
        "00000003-2", Incident.COLAPSO, geohash=gh, received_at=t0 + 10800.0
    )
    store.append(r2)
    ev2 = agente.procesar_reporte_normalizado(r2, store)
    assert ev2 is not None
    assert validate_event(ev2).link == "nuevo"


def test_antiguedad_maxima_de_grupo_superada() -> None:
    store = InMemoryEventStore()
    agente = Correlacionador("A", SeqGen())
    gh = encode_geohash(5.0695, -75.5180)

    t0 = 1794573600.0
    r1 = make_reporte_normalizado_event("00000003-1", Incident.COLAPSO, geohash=gh, received_at=t0)
    store.append(r1)
    agente.procesar_reporte_normalizado(r1, store)

    # Reporte 2 a 5 horas
    r2 = make_reporte_normalizado_event(
        "00000003-2", Incident.COLAPSO, geohash=gh, received_at=t0 + 18000.0
    )
    store.append(r2)
    agente.procesar_reporte_normalizado(r2, store)

    # Reporte 3 a 7 horas desde el inicio (> MAX_GROUP_AGE_SECONDS de 6 horas)
    r3 = make_reporte_normalizado_event(
        "00000003-3", Incident.COLAPSO, geohash=gh, received_at=t0 + 25200.0
    )
    store.append(r3)
    ev3 = agente.procesar_reporte_normalizado(r3, store)
    assert ev3 is not None
    assert validate_event(ev3).link == "nuevo"


# --- Compatibilidad de Tipos de Incidente ----------------------------------------------


def test_tipos_incompatibles_en_misma_ubicacion_resulta_nuevo() -> None:
    store = InMemoryEventStore()
    agente = Correlacionador("A", SeqGen())
    gh = encode_geohash(5.0695, -75.5180)

    # Incendio y vía bloqueada no se agrupan
    r1 = make_reporte_normalizado_event("00000004-1", Incident.INCENDIO, geohash=gh)
    store.append(r1)
    agente.procesar_reporte_normalizado(r1, store)

    r2 = make_reporte_normalizado_event("00000004-2", Incident.VIA_BLOQUEADA, geohash=gh)
    store.append(r2)
    ev2 = agente.procesar_reporte_normalizado(r2, store)
    assert ev2 is not None
    assert validate_event(ev2).link == "nuevo"


def test_compatibilidad_con_todos_los_miembros_del_grupo() -> None:
    store = InMemoryEventStore()
    agente = Correlacionador("A", SeqGen())
    gh = encode_geohash(5.0695, -75.5180)

    # Grupo con COLAPSO y ATRAPADOS
    r1 = make_reporte_normalizado_event("00000005-1", Incident.COLAPSO, geohash=gh, radius_m=100)
    store.append(r1)
    agente.procesar_reporte_normalizado(r1, store)

    r2 = make_reporte_normalizado_event("00000005-2", Incident.ATRAPADOS, geohash=gh, radius_m=100)
    store.append(r2)
    agente.procesar_reporte_normalizado(r2, store)

    # VIA_BLOQUEADA es compatible con COLAPSO (media), pero INCOMPATIBLE con ATRAPADOS
    r3 = make_reporte_normalizado_event(
        "00000005-3", Incident.VIA_BLOQUEADA, geohash=gh, radius_m=100
    )
    store.append(r3)
    ev3 = agente.procesar_reporte_normalizado(r3, store)
    assert ev3 is not None
    # Al no ser compatible con TODOS los miembros, debe resultar nuevo
    assert validate_event(ev3).link == "nuevo"


def test_otro_con_otro_compatibilidad_media() -> None:
    store = InMemoryEventStore()
    agente = Correlacionador("A", SeqGen())
    gh = encode_geohash(5.0695, -75.5180)

    r1 = make_reporte_normalizado_event("00000006-1", Incident.OTRO, geohash=gh, radius_m=50)
    store.append(r1)
    agente.procesar_reporte_normalizado(r1, store)

    # OTRO con OTRO es compatibilidad MEDIA -> en geohash sin texto confirmatorio queda dudoso
    r2 = make_reporte_normalizado_event("00000006-2", Incident.OTRO, geohash=gh, radius_m=50)
    store.append(r2)
    ev2 = agente.procesar_reporte_normalizado(r2, store)
    assert ev2 is not None
    p2 = validate_event(ev2)
    assert p2.link == "dudoso"


def test_rechaza_evento_que_no_sea_reporte_normalizado() -> None:
    store = InMemoryEventStore()
    agente = Correlacionador("A", SeqGen())

    ev_invalido = Event(
        origin_node="A",
        seq=1,
        category=EventCategory.HECHO,
        type="reporte",
        payload={},
        received_at=1794573600.0,
    )
    with pytest.raises(ValueError, match="Se esperaba evento reporte_normalizado"):
        agente.procesar_reporte_normalizado(ev_invalido, store)


def test_correlacionar_reporte_convenience_function() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen()
    r = make_reporte_normalizado_event("00000007-1", Incident.COLAPSO, barrio=5)
    store.append(r)
    ev = correlacionar_reporte(r, store, "A", seq_gen)
    assert ev is not None
    assert validate_event(ev).incident_id == "00000007-1"
