"""Evaluación cuantitativa de la calidad de búsqueda del RAG (D8).

Diseño: docs/diseno.md §13 (~20 preguntas con la sección esperada;
se mide si aparece entre los 3 primeros).
Plan: docs/plan.md (tarea D8).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from guadua.knowledge.hybrid_retriever import HybridRetriever
from guadua.knowledge.retriever import Retriever


@dataclass(frozen=True, slots=True)
class PreguntaPrueba:
    """Una pregunta de prueba del benchmark con su sección oficial esperada."""

    pregunta: str
    secciones_esperadas: tuple[str, ...]
    documento_esperado: str
    tema: str


@dataclass(frozen=True, slots=True)
class ResultadoEvalRAG:
    """Métricas consolidadas de la evaluación de búsqueda del RAG."""

    total_preguntas: int
    hits_top_1: int
    hits_top_3: int
    hit_rate_top_1: float
    hit_rate_top_3: float
    mrr: float  # Mean Reciprocal Rank
    detalles: tuple[dict[str, Any], ...]


# Benchmark curado de 21 preguntas de respuesta a emergencias en Manizales
BENCHMARK_RAG: tuple[PreguntaPrueba, ...] = (
    PreguntaPrueba(
        pregunta="fuga de gas con olor penetrante en sector residencial",
        secciones_esperadas=("§3.2", "§2.9"),
        documento_esperado="Protocolo de Emergencia por Fugas de Gas / EFIGAS",
        tema="Fuga de gas y prevención de explosión",
    ),
    PreguntaPrueba(
        pregunta="protocolo de búsqueda y rescate en estructuras colapsadas "
        "con apuntalamiento USAR",
        secciones_esperadas=("§3.1", "§2.4"),
        documento_esperado="Protocolo USAR / Bomberos",
        tema="Estructuras colapsadas y apuntalamiento",
    ),
    PreguntaPrueba(
        pregunta="entidad líder en rescate y extracción de personas atrapadas bajo escombros",
        secciones_esperadas=("§2.4", "§3.1"),
        documento_esperado="Cuerpo Oficial de Bomberos",
        tema="Rescate de personas atrapadas",
    ),
    PreguntaPrueba(
        pregunta="cierre y desvío de vías para paso prioritario de ambulancias",
        secciones_esperadas=("§2.3",),
        documento_esperado="Secretaría de Movilidad",
        tema="Corredores de emergencia vial",
    ),
    PreguntaPrueba(
        pregunta="cables de alta tensión caídos en la calle con chispas",
        secciones_esperadas=("§2.10",),
        documento_esperado="CHEC",
        tema="Aislamiento de energía eléctrica",
    ),
    PreguntaPrueba(
        pregunta="carrotanques de agua potable para abastecer barrio sin servicio",
        secciones_esperadas=("§2.8",),
        documento_esperado="Aguas de Manizales",
        tema="Suministro de agua de emergencia",
    ),
    PreguntaPrueba(
        pregunta="acordonamiento perimetral y seguridad para evitar saqueos",
        secciones_esperadas=("§2.1",),
        documento_esperado="Policía Nacional",
        tema="Seguridad y orden público",
    ),
    PreguntaPrueba(
        pregunta="montaje de albergue temporal y carpas para familias damnificadas",
        secciones_esperadas=("§2.7", "§3.5"),
        documento_esperado="Defensa Civil / Albergues",
        tema="Alojamientos temporales y albergues",
    ),
    PreguntaPrueba(
        pregunta="atención prehospitalaria APH y apoyo psicosocial a víctimas",
        secciones_esperadas=("§2.6",),
        documento_esperado="Cruz Roja Colombiana",
        tema="Atención prehospitalaria y salud mental",
    ),
    PreguntaPrueba(
        pregunta="maquinaria pesada retroexcavadora para remoción de escombros en la vía",
        secciones_esperadas=("§2.13",),
        documento_esperado="Secretaría de Infraestructura",
        tema="Maquinaria amarilla y despeje vial",
    ),
    PreguntaPrueba(
        pregunta="evaluación técnica de estabilidad de ladera por riesgo de derrumbe",
        secciones_esperadas=("§2.11",),
        documento_esperado="CORPOCALDAS",
        tema="Riesgo geológico y laderas",
    ),
    PreguntaPrueba(
        pregunta="derrame de sustancias químicas tóxicas e inflamables en bodega",
        secciones_esperadas=("§2.5",),
        documento_esperado="Bomberos HazMat",
        tema="Materiales peligrosos",
    ),
    PreguntaPrueba(
        pregunta="triage médico extrahospitalario y regulación de ambulancias a hospitales",
        secciones_esperadas=("§2.12", "§3.6"),
        documento_esperado="Secretaría de Salud / CRUE",
        tema="Red de urgencias hospitalarias",
    ),
    PreguntaPrueba(
        pregunta="activación del puesto de mando unificado PMU por emergencia mayor",
        secciones_esperadas=("§1.2",),
        documento_esperado="EMRE Documento Principal",
        tema="Activación del PMU",
    ),
    PreguntaPrueba(
        pregunta="declaratoria municipal de calamidad pública por desastre",
        secciones_esperadas=("§1.3",),
        documento_esperado="EMRE Documento Principal",
        tema="Calamidad pública",
    ),
    PreguntaPrueba(
        pregunta="apoyo del ejército con transporte pesado y camiones militares",
        secciones_esperadas=("§2.2",),
        documento_esperado="Batallón Ayacucho / UTAC",
        tema="Apoyo militar y transporte pesado",
    ),
    PreguntaPrueba(
        pregunta="recolección de escombros y residuos no contaminados",
        secciones_esperadas=("§2.14",),
        documento_esperado="EMAS",
        tema="Disposición de escombros",
    ),
    PreguntaPrueba(
        pregunta="levantamiento e identificación de cadáveres en la escena",
        secciones_esperadas=("§2.15",),
        documento_esperado="Medicina Legal / CTI",
        tema="Manejo forense de personas fallecidas",
    ),
    PreguntaPrueba(
        pregunta="comunicación radial en frecuencia VHF por caída de señal celular",
        secciones_esperadas=("§3.7", "§1.4"),
        documento_esperado="Protocolo VHF / Telecomunicaciones",
        tema="Comunicaciones de emergencia VHF",
    ),
    PreguntaPrueba(
        pregunta="grietas en la corona del talud y alarma por deslizamiento de tierra",
        secciones_esperadas=("§3.3",),
        documento_esperado="Protocolo Deslizamientos",
        tema="Alarma de deslizamiento de tierra",
    ),
    PreguntaPrueba(
        pregunta="rescate con cuerdas en cañada o ladera empinada de difícil acceso",
        secciones_esperadas=("§3.4",),
        documento_esperado="Protocolo Rescate Vertical",
        tema="Rescate vertical y agreste",
    ),
    PreguntaPrueba(
        pregunta="clasificación rápida de víctimas con código de colores START",
        secciones_esperadas=("§3.6",),
        documento_esperado="Protocolo Triage START",
        tema="Clasificación de heridos en masa",
    ),
)


def evaluar_rag(retriever: Retriever | None = None) -> ResultadoEvalRAG:
    """Evalúa la calidad de recuperación del RAG sobre el benchmark oficial.

    Métricas calculadas:
    - Hit@1: % de preguntas con la sección esperada en la primera posición.
    - Hit@3: % de preguntas con la sección esperada entre los primeros 3 resultados.
    - MRR (Mean Reciprocal Rank): 1 / rango_primer_acierto promedio.
    """
    ret = retriever if retriever is not None else HybridRetriever.default()

    total = len(BENCHMARK_RAG)
    hits_1 = 0
    hits_3 = 0
    suma_reciprocal_rank = 0.0
    detalles: list[dict[str, Any]] = []

    for item in BENCHMARK_RAG:
        resultados = ret.search(item.pregunta, k=3)
        secciones_obtenidas = [p.section for p in resultados]

        rank_acierto = None
        for rank_pos, seccion in enumerate(secciones_obtenidas, start=1):
            if seccion in item.secciones_esperadas:
                rank_acierto = rank_pos
                break

        en_top_1 = rank_acierto == 1
        en_top_3 = rank_acierto is not None and rank_acierto <= 3

        if en_top_1:
            hits_1 += 1
        if en_top_3:
            hits_3 += 1

        if rank_acierto is not None:
            suma_reciprocal_rank += 1.0 / rank_acierto

        detalles.append(
            {
                "pregunta": item.pregunta,
                "tema": item.tema,
                "esperadas": item.secciones_esperadas,
                "obtenidas": secciones_obtenidas,
                "rank_acierto": rank_acierto,
                "en_top_3": en_top_3,
            }
        )

    hit_rate_1 = hits_1 / total if total > 0 else 0.0
    hit_rate_3 = hits_3 / total if total > 0 else 0.0
    mrr = suma_reciprocal_rank / total if total > 0 else 0.0

    return ResultadoEvalRAG(
        total_preguntas=total,
        hits_top_1=hits_1,
        hits_top_3=hits_3,
        hit_rate_top_1=hit_rate_1,
        hit_rate_top_3=hit_rate_3,
        mrr=mrr,
        detalles=tuple(detalles),
    )


if __name__ == "__main__":
    resultado = evaluar_rag()
    pct_1 = resultado.hit_rate_top_1 * 100
    pct_3 = resultado.hit_rate_top_3 * 100
    print("=== Evaluación RAG Guadua (D8) ===")
    print(f"Total preguntas : {resultado.total_preguntas}")
    print(f"Hit@1           : {resultado.hits_top_1}/{resultado.total_preguntas} ({pct_1:.1f} %)")
    print(f"Hit@3           : {resultado.hits_top_3}/{resultado.total_preguntas} ({pct_3:.1f} %)")
    print(f"MRR             : {resultado.mrr:.3f}")
