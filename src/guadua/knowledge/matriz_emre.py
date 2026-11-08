"""Módulo de conocimiento: Matriz EMRE y Recursos.

Carga la matriz de roles y responsabilidades de Manizales (EMRE 2024) y la tabla de recursos.
CONTRATO: funciones puras que consultan la matriz y recursos sin estado mutable.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from guadua.protocol.header import Incident
from guadua.store.event_types import Citation

_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
_MATRIZ_CSV = _DEFAULT_DATA_DIR / "matriz_emre.csv"
_RECURSOS_CSV = _DEFAULT_DATA_DIR / "recursos_ejemplo.csv"


@dataclass(frozen=True, slots=True)
class EntradaMatriz:
    tipo_incidente: int
    tarea: str
    entidad: str
    rol: str  # "L" o "R"
    documento: str
    seccion: str
    es_ejemplo: bool


@dataclass(frozen=True, slots=True)
class EntradaRecurso:
    entidad: str
    unidades_total: int
    unidades_disponibles: int
    es_ejemplo: bool


def _cargar_matriz(ruta_csv: Path | str | None = None) -> list[EntradaMatriz]:
    path = Path(ruta_csv) if ruta_csv else _MATRIZ_CSV
    if not path.is_file():
        path = Path("data/matriz_emre.csv")
    if not path.is_file():
        return []

    entradas: list[EntradaMatriz] = []
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            entradas.append(
                EntradaMatriz(
                    tipo_incidente=int(row["tipo_incidente"]),
                    tarea=row["tarea"],
                    entidad=row["entidad"],
                    rol=row["rol"].strip().upper(),
                    documento=row["documento"],
                    seccion=row["seccion"],
                    es_ejemplo=row["es_ejemplo"].strip().lower() in ("true", "1", "yes"),
                )
            )
    return entradas


def _cargar_recursos(ruta_csv: Path | str | None = None) -> dict[str, EntradaRecurso]:
    path = Path(ruta_csv) if ruta_csv else _RECURSOS_CSV
    if not path.is_file():
        path = Path("data/recursos_ejemplo.csv")
    if not path.is_file():
        return {}

    recursos: dict[str, EntradaRecurso] = {}
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            entidad = row["entidad"].strip()
            recursos[entidad] = EntradaRecurso(
                entidad=entidad,
                unidades_total=int(row["unidades_total"]),
                unidades_disponibles=int(row["unidades_disponibles"]),
                es_ejemplo=row["es_ejemplo"].strip().lower() in ("true", "1", "yes"),
            )
    return recursos


def responsable_por_tipo(
    tipo: Incident | int,
    excluidos: Sequence[str] | set[str] | None = None,
    ruta_csv: Path | str | None = None,
) -> str | None:
    """Devuelve la entidad responsable para un tipo de incidente según la EMRE.

    Regla: Líder (L) de la tarea; si no hay L (o está excluido), el primer Responsable (R).
    Si no hay entidad disponible o el tipo no tiene responsable, devuelve None.
    """
    tipo_int = int(tipo)
    entradas = [e for e in _cargar_matriz(ruta_csv) if e.tipo_incidente == tipo_int]
    if not entradas:
        return None

    excluidos_set = set(excluidos) if excluidos else set()

    # 1. Buscar Líder (L) no excluido
    for e in entradas:
        if e.rol == "L" and e.entidad not in excluidos_set:
            return e.entidad

    # 2. Si no hay L o fue excluido, buscar primer Responsable (R) no excluido
    for e in entradas:
        if e.rol == "R" and e.entidad not in excluidos_set:
            return e.entidad

    return None


def cita_por_tipo(
    tipo: Incident | int,
    entidad: str | None = None,
    ruta_csv: Path | str | None = None,
) -> Citation | None:
    """Devuelve la cita oficial correspondiente a la asignación de la EMRE."""
    tipo_int = int(tipo)
    entradas = [e for e in _cargar_matriz(ruta_csv) if e.tipo_incidente == tipo_int]
    if not entradas:
        return None

    entrada_sel = None
    if entidad:
        for e in entradas:
            if e.entidad == entidad:
                entrada_sel = e
                break

    if entrada_sel is None:
        entrada_sel = entradas[0]

    return Citation(
        document=entrada_sel.documento,
        section=entrada_sel.seccion,
        version="Actualización 2024",
        is_example=entrada_sel.es_ejemplo,
    )


def recursos_disponibles(
    entidad: str,
    ruta_csv: Path | str | None = None,
) -> int | None:
    """Devuelve el número de unidades disponibles de una entidad o None si no está registrada."""
    recursos = _cargar_recursos(ruta_csv)
    entrada = recursos.get(entidad.strip())
    if entrada is None:
        return None
    return entrada.unidades_disponibles
