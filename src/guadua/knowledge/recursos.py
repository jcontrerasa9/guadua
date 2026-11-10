"""Gestión de recursos y unidades por entidad para coordinación de emergencias (D8).

Diseño: docs/diseno.md §13 (tabla editable de unidades: disponible, en misión, fuera de servicio).
Plan: docs/plan.md (tarea D8).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
_RECURSOS_CSV = _DEFAULT_DATA_DIR / "recursos_ejemplo.csv"


@dataclass(frozen=True, slots=True)
class InfoRecurso:
    """Estado y conteo operativo de unidades de una entidad."""

    entidad: str
    unidades_total: int
    unidades_disponibles: int
    unidades_en_mision: int = 0
    unidades_fuera_servicio: int = 0
    es_ejemplo: bool = True

    @property
    def disponible(self) -> bool:
        """Indica si la entidad tiene al menos una unidad disponible."""
        return self.unidades_disponibles > 0


class TablaRecursos:
    """Tabla editable de unidades por entidad con seguimiento de estados operativos.

    Estados por unidad:
    - disponible: lista para asignación o despacho inmediato.
    - en_misión: actualmente atendiendo un incidente en terreno.
    - fuera_de_servicio: en mantenimiento, averiada o sin dotación.
    """

    def __init__(self, recursos: dict[str, InfoRecurso] | None = None) -> None:
        self._recursos: dict[str, InfoRecurso] = dict(recursos) if recursos else {}

    @classmethod
    def desde_csv(cls, ruta_csv: Path | str | None = None) -> TablaRecursos:
        """Carga la tabla de recursos desde un archivo CSV."""
        path = Path(ruta_csv) if ruta_csv else _RECURSOS_CSV
        if not path.is_file():
            path = Path("data/recursos_ejemplo.csv")
        if not path.is_file():
            return cls({})

        recursos: dict[str, InfoRecurso] = {}
        with path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                entidad = row["entidad"].strip()
                total = int(row.get("unidades_total", 0))
                disp = int(row.get("unidades_disponibles", 0))

                # Soporte para CSV extendido con en_mision y fuera_servicio
                if "unidades_en_mision" in row:
                    mision = int(row["unidades_en_mision"])
                else:
                    mision = max(0, total - disp)

                if "unidades_fuera_servicio" in row:
                    fuera = int(row["unidades_fuera_servicio"])
                else:
                    fuera = max(0, total - disp - mision)

                es_ej = row.get("es_ejemplo", "true").strip().lower() in ("true", "1", "yes")
                recursos[entidad] = InfoRecurso(
                    entidad=entidad,
                    unidades_total=total,
                    unidades_disponibles=disp,
                    unidades_en_mision=mision,
                    unidades_fuera_servicio=fuera,
                    es_ejemplo=es_ej,
                )
        return cls(recursos)

    def guardar_csv(self, ruta_csv: Path | str) -> None:
        """Persiste la tabla actual en formato CSV."""
        path = Path(ruta_csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        campos = [
            "entidad",
            "unidades_total",
            "unidades_disponibles",
            "unidades_en_mision",
            "unidades_fuera_servicio",
            "es_ejemplo",
        ]
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=campos)
            writer.writeheader()
            for rec in self._recursos.values():
                writer.writerow(
                    {
                        "entidad": rec.entidad,
                        "unidades_total": rec.unidades_total,
                        "unidades_disponibles": rec.unidades_disponibles,
                        "unidades_en_mision": rec.unidades_en_mision,
                        "unidades_fuera_servicio": rec.unidades_fuera_servicio,
                        "es_ejemplo": "true" if rec.es_ejemplo else "false",
                    }
                )

    def obtener(self, entidad: str) -> InfoRecurso | None:
        """Devuelve la información de recursos de la entidad o None si no existe."""
        return self._recursos.get(entidad.strip())

    def unidades_disponibles(self, entidad: str) -> int | None:
        """Devuelve el número de unidades disponibles de la entidad o None si no está registrada."""
        rec = self.obtener(entidad)
        return rec.unidades_disponibles if rec else None

    def despachar(self, entidad: str, n: int = 1) -> bool:
        """Mueve n unidades de disponibles a en_misión. Retorna True si fue posible."""
        rec = self.obtener(entidad)
        if not rec or rec.unidades_disponibles < n or n <= 0:
            return False

        self._recursos[rec.entidad] = InfoRecurso(
            entidad=rec.entidad,
            unidades_total=rec.unidades_total,
            unidades_disponibles=rec.unidades_disponibles - n,
            unidades_en_mision=rec.unidades_en_mision + n,
            unidades_fuera_servicio=rec.unidades_fuera_servicio,
            es_ejemplo=rec.es_ejemplo,
        )
        return True

    def liberar(self, entidad: str, n: int = 1) -> bool:
        """Mueve n unidades de en_misión de vuelta a disponibles. Retorna True si fue posible."""
        rec = self.obtener(entidad)
        if not rec or rec.unidades_en_mision < n or n <= 0:
            return False

        self._recursos[rec.entidad] = InfoRecurso(
            entidad=rec.entidad,
            unidades_total=rec.unidades_total,
            unidades_disponibles=rec.unidades_disponibles + n,
            unidades_en_mision=rec.unidades_en_mision - n,
            unidades_fuera_servicio=rec.unidades_fuera_servicio,
            es_ejemplo=rec.es_ejemplo,
        )
        return True

    def marcar_fuera_servicio(self, entidad: str, n: int = 1) -> bool:
        """Mueve n unidades de disponibles hacia fuera de servicio."""
        rec = self.obtener(entidad)
        if not rec or rec.unidades_disponibles < n or n <= 0:
            return False

        self._recursos[rec.entidad] = InfoRecurso(
            entidad=rec.entidad,
            unidades_total=rec.unidades_total,
            unidades_disponibles=rec.unidades_disponibles - n,
            unidades_en_mision=rec.unidades_en_mision,
            unidades_fuera_servicio=rec.unidades_fuera_servicio + n,
            es_ejemplo=rec.es_ejemplo,
        )
        return True

    def reintegrar_servicio(self, entidad: str, n: int = 1) -> bool:
        """Mueve n unidades de fuera de servicio a disponibles."""
        rec = self.obtener(entidad)
        if not rec or rec.unidades_fuera_servicio < n or n <= 0:
            return False

        self._recursos[rec.entidad] = InfoRecurso(
            entidad=rec.entidad,
            unidades_total=rec.unidades_total,
            unidades_disponibles=rec.unidades_disponibles + n,
            unidades_en_mision=rec.unidades_en_mision,
            unidades_fuera_servicio=rec.unidades_fuera_servicio - n,
            es_ejemplo=rec.es_ejemplo,
        )
        return True

    def editar(
        self,
        entidad: str,
        *,
        disponibles: int | None = None,
        en_mision: int | None = None,
        fuera_servicio: int | None = None,
        total: int | None = None,
        es_ejemplo: bool | None = None,
    ) -> InfoRecurso:
        """Edita directamente los valores de recursos para una entidad existente o nueva."""
        entidad_norm = entidad.strip()
        rec_actual = self.obtener(entidad_norm)

        disp = (
            disponibles
            if disponibles is not None
            else (rec_actual.unidades_disponibles if rec_actual else 0)
        )
        mis = (
            en_mision
            if en_mision is not None
            else (rec_actual.unidades_en_mision if rec_actual else 0)
        )
        fs = (
            fuera_servicio
            if fuera_servicio is not None
            else (rec_actual.unidades_fuera_servicio if rec_actual else 0)
        )
        tot = total if total is not None else (disp + mis + fs)
        ej = (
            es_ejemplo
            if es_ejemplo is not None
            else (rec_actual.es_ejemplo if rec_actual else True)
        )

        nuevo = InfoRecurso(
            entidad=entidad_norm,
            unidades_total=tot,
            unidades_disponibles=disp,
            unidades_en_mision=mis,
            unidades_fuera_servicio=fs,
            es_ejemplo=ej,
        )
        self._recursos[entidad_norm] = nuevo
        return nuevo

    def todas(self) -> dict[str, InfoRecurso]:
        """Retorna una copia del diccionario de todas las entidades y recursos."""
        return dict(self._recursos)

    def listar(self) -> list[InfoRecurso]:
        """Lista ordenada alfabéticamente de todas las entidades."""
        return sorted(self._recursos.values(), key=lambda r: r.entidad)
