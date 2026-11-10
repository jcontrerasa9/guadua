"""Pruebas del RAG híbrido (FTS5 + embeddings) y tabla de recursos (D8).

Verifica:
- Cumplimiento estricto del contrato: HybridRetriever implementa Retriever y retorna Passage.
- Búsqueda léxica SQLite FTS5 (términos exactos, prefijos, caracteres especiales).
- Búsqueda semántica densa (embeddings locales con normalización L2 y similitud coseno).
- Fusión híbrida RRF (Reciprocal Rank Fusion) para combinar léxico y semántico.
- Benchmark oficial de calidad: las 22 preguntas tienen su sección esperada en el top 3.
- Tabla de recursos: seguimiento de estados (disponible, en misión, fuera de servicio),
  despacho, liberación, edición y persistencia CSV.
- Integración con AgenteCoordinacion: el RAG provee citas que se incorporan a la ficha.
- Todo funciona 100% offline sin internet ni API key (Regla 3 de AGENTS.md).
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

from guadua.agents.coordinacion import AgenteCoordinacion
from guadua.knowledge.eval_rag import BENCHMARK_RAG, evaluar_rag, evaluar_rag_oficial
from guadua.knowledge.hybrid_retriever import HybridRetriever, _limpiar_para_fts
from guadua.knowledge.recursos import InfoRecurso, TablaRecursos
from guadua.knowledge.retriever import Passage, Retriever
from guadua.protocol.header import Incident
from guadua.store.event_types import Ficha, IncidenteAgrupado, ReporteNormalizado, validate_event
from tests.fakes import InMemoryEventStore, NullLLM


class SeqGen:
    def __init__(self, start: int = 1) -> None:
        self._counter = itertools.count(start)

    def __call__(self) -> int:
        return next(self._counter)


def make_reporte_norm(report_id: str, incident: Incident, seq: int = 1) -> ReporteNormalizado:
    ev = ReporteNormalizado.model_validate(
        {
            "report_id": report_id,
            "incident": incident,
            "people": 1,
            "location": {"geohash": "d2fkcdc", "radius_m": 150, "label": "El Cable"},
            "direct_observation": True,
            "vital_signs": ["atrapados"],
            "risk_signals": ["gas"],
            "text": "reporte de prueba",
            "unknown_fields": [],
        }
    )
    # Convertir a Event
    from guadua.store.events import Event, EventCategory

    return Event(
        origin_node="A",
        seq=seq,
        category=EventCategory.DERIVADO,
        type="reporte_normalizado",
        payload=ev.model_dump(mode="json"),
        refs=(),
        received_at=1000.0,
    )


def make_incidente_agrupado_ev(incident_id: str, report_ids: tuple[str, ...], seq: int = 2):
    inc = IncidenteAgrupado(
        incident_id=incident_id,
        report_ids=report_ids,
        link="nuevo",
        confidence=0.9,
    )
    from guadua.store.events import Event, EventCategory

    return Event(
        origin_node="A",
        seq=seq,
        category=EventCategory.DERIVADO,
        type="incidente_agrupado",
        payload=inc.model_dump(mode="json"),
        refs=(),
        received_at=1000.0,
    )


# --- Pruebas de Cumplimiento de Contrato -----------------------------------------------


def test_hybrid_retriever_cumple_protocolo() -> None:
    retriever = HybridRetriever(":memory:")
    assert isinstance(retriever, Retriever)

    pasajes = [
        Passage("Doc A", "§1", "2024", "procedimiento de evacuacion de gas", score=0.9),
        Passage("Doc B", "§2", "2024", "rescate en estructuras colapsadas", score=0.8),
    ]
    retriever.add_passages(pasajes)

    res = retriever.search("gas", k=1)
    assert len(res) == 1
    assert isinstance(res[0], Passage)
    assert res[0].section == "§1"
    assert res[0].document == "Doc A"
    assert 0.0 <= res[0].score <= 1.0


def test_limpiar_para_fts_caracteres_especiales() -> None:
    # No debe fallar con puntuación, comillas ni caracteres no alfanuméricos
    limpio = _limpiar_para_fts('¿¡fuga de "gas"! en: sector; (alerta)*?')
    assert "fuga" in limpio
    assert "gas" in limpio
    assert "sector" in limpio
    assert "alerta" in limpio
    assert "?" not in limpio
    assert "!" not in limpio


# --- Pruebas de Búsqueda FTS5 y Semántica ----------------------------------------------


def test_busqueda_fts_exacta_y_prefijos() -> None:
    retriever = HybridRetriever(":memory:")
    pasajes = [
        Passage("EMRE", "§1.1", "2024", "deslizamiento de tierra en la ladera sur", score=0.0),
        Passage("EMRE", "§1.2", "2024", "fuga de gas en red matriz subterranea", score=0.0),
        Passage("EMRE", "§1.3", "2024", "colapso estructural de vivienda de tres pisos", score=0.0),
    ]
    retriever.add_passages(pasajes)

    # Consulta con prefijo y palabra exacta
    res_gas = retriever.search("gas subterránea", k=1)
    assert res_gas[0].section == "§1.2"

    res_colapso = retriever.search("colaps*", k=1)
    assert res_colapso[0].section == "§1.3"


def test_busqueda_semantica_sinonimos() -> None:
    retriever = HybridRetriever(":memory:")
    pasajes = [
        Passage(
            "EMRE",
            "§2.9",
            "2024",
            "Efigas deteccion y suspension de gas natural combustible",
            score=0.0,
        ),
        Passage(
            "EMRE",
            "§2.4",
            "2024",
            "Bomberos rescate de personas atrapadas en escombros",
            score=0.0,
        ),
        Passage(
            "EMRE",
            "§2.10",
            "2024",
            "Chec aislamiento de redes de energia electrica",
            score=0.0,
        ),
    ]
    retriever.add_passages(pasajes)

    # Consulta usando paráfrasis ("tubería con hidrocarburo volátil inflamable")
    res = retriever.search("tubería de combustible gas inflamable", k=1)
    assert res[0].section == "§2.9"


# --- Pruebas del Benchmark Oficial de Evaluación (D8) ----------------------------------


def test_benchmark_rag_cumple_criterio_top_3() -> None:
    """Criterio de éxito D8: ~20 preguntas, sección esperada en el top 3.

    Exigimos que Hit@3 sea superior al 95 % (en nuestro benchmark alcanza el 100 %).
    """
    retriever = HybridRetriever.default()
    resultado = evaluar_rag(retriever)

    # El benchmark contiene más de 20 preguntas (22 en total)
    assert resultado.total_preguntas >= 20
    # Verificamos que Hit@3 supere con creces el criterio exigido
    assert resultado.hit_rate_top_3 >= 0.95
    assert resultado.hits_top_3 >= 21
    # medido: 0,886. Con ~700 pasajes oficiales en el mismo índice, a veces gana una página
    # oficial que también responde; el benchmark oficial lo mide aparte.
    assert resultado.mrr >= 0.85

    # Verificar que cada pregunta individualmente tenga su sección esperada en el top 3
    for det in resultado.detalles:
        f_msg = f"Fallo en: {det['pregunta']} -> Obtenidas: {det['obtenidas']}"
        assert det["en_top_3"], f_msg


def test_benchmark_cubre_todas_las_entidades_principales() -> None:
    """Verifica que el benchmark contemple las instituciones clave de Manizales."""
    temas = {item.tema for item in BENCHMARK_RAG}
    assert any("gas" in t.lower() for t in temas)
    assert any("escombros" in t.lower() or "colaps" in t.lower() for t in temas)
    assert any("vial" in t.lower() or "movilidad" in t.lower() for t in temas)
    assert any("eléctrica" in t.lower() or "energia" in t.lower() for t in temas)
    assert any("agua" in t.lower() for t in temas)
    assert any("albergue" in t.lower() for t in temas)
    assert any("prehospitalaria" in t.lower() or "triage" in t.lower() for t in temas)
    assert any("pmu" in t.lower() for t in temas)


# --- Pruebas de la Tabla de Recursos --------------------------------------------------


def test_tabla_recursos_estados_y_transiciones() -> None:
    recursos = {
        "Bomberos": InfoRecurso(
            entidad="Bomberos",
            unidades_total=10,
            unidades_disponibles=6,
            unidades_en_mision=3,
            unidades_fuera_servicio=1,
        )
    }
    tabla = TablaRecursos(recursos)
    info = tabla.obtener("Bomberos")
    assert info is not None
    assert info.disponible is True
    assert info.unidades_disponibles == 6
    assert info.unidades_en_mision == 3
    assert info.unidades_fuera_servicio == 1

    # Despachar 2 unidades a misión
    assert tabla.despachar("Bomberos", 2) is True
    assert tabla.unidades_disponibles("Bomberos") == 4
    assert tabla.obtener("Bomberos").unidades_en_mision == 5

    # Liberar 1 unidad de misión
    assert tabla.liberar("Bomberos", 1) is True
    assert tabla.unidades_disponibles("Bomberos") == 5
    assert tabla.obtener("Bomberos").unidades_en_mision == 4

    # Marcar 1 unidad fuera de servicio
    assert tabla.marcar_fuera_servicio("Bomberos", 1) is True
    assert tabla.unidades_disponibles("Bomberos") == 4
    assert tabla.obtener("Bomberos").unidades_fuera_servicio == 2

    # Reintegrar 1 unidad a disponible
    assert tabla.reintegrar_servicio("Bomberos", 1) is True
    assert tabla.unidades_disponibles("Bomberos") == 5
    assert tabla.obtener("Bomberos").unidades_fuera_servicio == 1


def test_tabla_recursos_edicion_directa() -> None:
    tabla = TablaRecursos()
    tabla.editar("Cruz Roja", disponibles=4, en_mision=2, fuera_servicio=1)

    cr = tabla.obtener("Cruz Roja")
    assert cr is not None
    assert cr.unidades_disponibles == 4
    assert cr.unidades_en_mision == 2
    assert cr.unidades_fuera_servicio == 1
    assert cr.unidades_total == 7


def test_tabla_recursos_persistencia_csv(tmp_path: Path) -> None:
    archivo_csv = tmp_path / "recursos_test.csv"
    tabla = TablaRecursos()
    tabla.editar("Defensa Civil", disponibles=5, en_mision=1, fuera_servicio=0)
    tabla.editar("Efigas", disponibles=3, en_mision=2, fuera_servicio=1)

    tabla.guardar_csv(archivo_csv)
    assert archivo_csv.is_file()

    cargada = TablaRecursos.desde_csv(archivo_csv)
    assert cargada.unidades_disponibles("Defensa Civil") == 5
    assert cargada.obtener("Efigas").unidades_en_mision == 2


def test_tabla_recursos_agotamiento_advierte_escalado() -> None:
    tabla = TablaRecursos()
    tabla.editar("Entidad Vacia", disponibles=0, en_mision=3, fuera_servicio=0)
    info = tabla.obtener("Entidad Vacia")
    assert info.disponible is False
    assert tabla.unidades_disponibles("Entidad Vacia") == 0


# --- Integración con Agente de Coordinación -------------------------------------------


def test_integracion_coordinacion_con_hybrid_retriever() -> None:
    store = InMemoryEventStore()
    seq_gen = SeqGen(100)

    retriever = HybridRetriever.default()
    r = make_reporte_norm("a1b2c3d4-1", Incident.COLAPSO, seq=1)
    store.append(r)
    inc = make_incidente_agrupado_ev("a1b2c3d4-1", ("a1b2c3d4-1",), seq=2)
    store.append(inc)

    agente = AgenteCoordinacion("A", seq_gen, llm=NullLLM(), retriever=retriever)
    ficha_ev = agente.procesar_incidente(inc, store)

    assert ficha_ev is not None
    p = validate_event(ficha_ev)
    assert isinstance(p, Ficha)
    # Debe contener citas generadas por el RAG oficial
    assert len(p.citations) >= 1
    assert any("EMRE" in c.document or "Protocolo" in c.document for c in p.citations)


def test_benchmark_oficial_sobre_paginas_reales() -> None:
    """Preguntas cuya respuesta está en páginas verificadas de los PDF (data/fuentes)."""
    resultado = evaluar_rag_oficial(HybridRetriever.default())
    assert resultado.total_preguntas >= 15
    assert resultado.hit_rate_top_3 >= 0.75  # medido: 81,2 % (13/16)


def test_el_corpus_cita_paginas_reales_y_marca_los_resumenes() -> None:
    datos = json.loads((Path(__file__).parents[1] / "data" / "emre_passages.json").read_text())
    oficiales = [d for d in datos if not d["is_example"]]
    resumenes = [d for d in datos if d["is_example"]]
    assert len(oficiales) > 500
    assert all(d["section"].startswith("p. ") for d in oficiales)
    assert all(d["document"].startswith("EMRE Manizales 2024 - ") for d in oficiales)
    # Lo que no sale de un PDF oficial nunca se presenta como oficial
    assert resumenes and all("no oficial" in d["document"] for d in resumenes)


def test_coordinacion_cita_un_pasaje_oficial_y_uno_del_equipo() -> None:
    store = InMemoryEventStore()
    store.append(make_reporte_norm("a1b2c3d4-1", Incident.FUGA_GAS, seq=1))
    inc = make_incidente_agrupado_ev("a1b2c3d4-1", ("a1b2c3d4-1",), seq=2)
    store.append(inc)
    agente = AgenteCoordinacion("A", SeqGen(100), retriever=HybridRetriever.default())
    p = validate_event(agente.procesar_incidente(inc, store))
    rag = p.citations[1:]  # la primera es la de la matriz EMRE
    assert [c.is_example for c in rag] == [False, True]
    assert rag[0].section.startswith("p. ")
