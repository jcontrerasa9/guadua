"""Retriever híbrido (FTS5 + embeddings) para el RAG de Guadua (D8).

Diseño: docs/diseno.md §13 (búsqueda híbrida FTS5 + embeddings en SQLite).
Plan: docs/plan.md (tarea D8).
Interfaz: guadua.knowledge.retriever.Retriever (CONTRATO).

Combina:
1. Búsqueda léxica exacta y por prefijo usando SQLite FTS5 con BM25.
2. Búsqueda semántica densa basada en n-gramas TF-IDF con normalización L2 y similitud coseno.
3. Fusión híbrida por Reciprocal Rank Fusion (RRF) para ordenar los pasajes
   de mayor a menor relevancia.
4. Totalmente offline: no requiere internet, API keys ni descarga de modelos externos.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from guadua.knowledge.retriever import Passage, Retriever

_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
_DEFAULT_PASSAGES_JSON = _DEFAULT_DATA_DIR / "emre_passages.json"


def _limpiar_para_fts(texto: str) -> str:
    """Extrae palabras seguras para armar una consulta FTS5 sin errores de sintaxis."""
    palabras = re.findall(r"\w+", texto.lower())
    filtradas = [p for p in palabras if len(p) >= 2]
    if not filtradas:
        return ""
    # Coincidencia por palabra o prefijo con operador OR
    return " OR ".join(f'"{p}"*' for p in filtradas)


class HybridRetriever(Retriever):
    """Implementación de Retriever basada en SQLite FTS5 y embeddings locales."""

    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        w_fts: float = 0.5,
        w_sem: float = 0.5,
        k_rrf: int = 60,
    ) -> None:
        self.w_fts = w_fts
        self.w_sem = w_sem
        self.k_rrf = k_rrf

        self._con = sqlite3.connect(db_path)
        self._crear_tablas()

        self._vectorizer: TfidfVectorizer | None = None
        self._doc_embeddings: np.ndarray | None = None
        self._doc_ids: list[int] = []
        self._passages_cache: dict[int, Passage] = {}

    def _crear_tablas(self) -> None:
        with self._con:
            self._con.execute(
                """
                CREATE TABLE IF NOT EXISTS passages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document TEXT NOT NULL,
                    section TEXT NOT NULL,
                    version TEXT NOT NULL,
                    text TEXT NOT NULL,
                    is_example INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            self._con.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
                    text,
                    section,
                    document,
                    content='passages',
                    content_rowid='id'
                );
                """
            )

    def add_passage(self, passage: Passage) -> int:
        """Agrega un pasaje a la base de datos."""
        with self._con:
            cur = self._con.execute(
                """
                INSERT INTO passages (document, section, version, text, is_example)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    passage.document,
                    passage.section,
                    passage.version,
                    passage.text,
                    1 if passage.is_example else 0,
                ),
            )
            rowid = cur.lastrowid
            self._con.execute(
                """
                INSERT INTO passages_fts (rowid, text, section, document)
                VALUES (?, ?, ?, ?)
                """,
                (rowid, passage.text, passage.section, passage.document),
            )
        self._passages_cache[rowid] = passage
        return rowid

    def add_passages(self, passages: Iterable[Passage]) -> None:
        """Agrega múltiples pasajes e indexa automáticamente."""
        for p in passages:
            self.add_passage(p)
        self.index()

    def index(self) -> None:
        """Entrena y actualiza el espacio vectorial semántico con los pasajes actuales."""
        cur = self._con.execute(
            "SELECT id, document, section, version, text, is_example FROM passages ORDER BY id"
        )
        filas = cur.fetchall()
        if not filas:
            self._doc_embeddings = None
            self._doc_ids = []
            return

        self._doc_ids = [r[0] for r in filas]
        self._passages_cache = {
            r[0]: Passage(
                document=r[1],
                section=r[2],
                version=r[3],
                text=r[4],
                score=0.0,
                is_example=bool(r[5]),
            )
            for r in filas
        }

        # Corpus enriquecido combinando texto, sección y documento
        corpus = [f"{r[4]} {r[2]} {r[1]}" for r in filas]
        self._vectorizer = TfidfVectorizer(
            ngram_range=(1, 2),
            sublinear_tf=True,
            strip_accents="unicode",
        )
        matriz = self._vectorizer.fit_transform(corpus).toarray().astype(np.float32)
        normas = np.linalg.norm(matriz, axis=1, keepdims=True)
        normas[normas == 0] = 1.0
        self._doc_embeddings = matriz / normas

    def search(self, query: str, k: int = 3) -> list[Passage]:
        """Busca los k pasajes más relevantes combinando búsqueda FTS5 y semántica."""
        if not self._passages_cache:
            return []

        fts_ranks = self._buscar_fts(query)
        sem_ranks = self._buscar_semantico(query)

        # Fusión RRF (Reciprocal Rank Fusion)
        todos_los_ids = set(fts_ranks.keys()) | set(sem_ranks.keys())
        if not todos_los_ids:
            # Fallback a los primeros pasajes por ID si nada coincide
            todos_los_ids = set(self._doc_ids[:k])

        max_posible = (self.w_fts / self.k_rrf) + (self.w_sem / self.k_rrf)

        puntuaciones: list[tuple[float, int]] = []
        for pid in todos_los_ids:
            score = 0.0
            if pid in fts_ranks:
                score += self.w_fts / (self.k_rrf + fts_ranks[pid])
            if pid in sem_ranks:
                score += self.w_sem / (self.k_rrf + sem_ranks[pid])

            norm_score = min(1.0, score / max_posible) if max_posible > 0 else 0.0
            puntuaciones.append((norm_score, pid))

        # Ordenar de mayor a menor relevancia
        puntuaciones.sort(key=lambda item: item[0], reverse=True)

        resultados: list[Passage] = []
        for score, pid in puntuaciones[:k]:
            base = self._passages_cache[pid]
            resultados.append(
                Passage(
                    document=base.document,
                    section=base.section,
                    version=base.version,
                    text=base.text,
                    score=float(score),
                    is_example=base.is_example,
                )
            )
        return resultados

    def _buscar_fts(self, query: str) -> dict[int, int]:
        """Retorna diccionario de {doc_id: posicion_de_ranking_fts (0-indexed)}."""
        fts_q = _limpiar_para_fts(query)
        if not fts_q:
            return {}

        try:
            cur = self._con.execute(
                """
                SELECT rowid, bm25(passages_fts) as rank
                FROM passages_fts
                WHERE passages_fts MATCH ?
                ORDER BY rank
                LIMIT 50
                """,
                (fts_q,),
            )
            rows = cur.fetchall()
            return {r[0]: idx for idx, r in enumerate(rows)}
        except sqlite3.OperationalError:
            return {}

    def _buscar_semantico(self, query: str) -> dict[int, int]:
        """Retorna diccionario de {doc_id: posicion_de_ranking_semantico (0-indexed)}."""
        if self._vectorizer is None or self._doc_embeddings is None or not self._doc_ids:
            return {}

        q_vec = self._vectorizer.transform([query]).toarray().astype(np.float32)
        norma = np.linalg.norm(q_vec)
        if norma > 0:
            q_vec = q_vec / norma
        else:
            return {}

        # Similitud coseno mediante producto punto
        similitudes = np.dot(self._doc_embeddings, q_vec.T).flatten()
        indices_ordenados = np.argsort(similitudes)[::-1]

        ranking: dict[int, int] = {}
        for rank_pos, idx in enumerate(indices_ordenados):
            if similitudes[idx] <= 0.0:
                continue
            doc_id = self._doc_ids[idx]
            ranking[doc_id] = rank_pos
        return ranking

    @classmethod
    def from_passages(
        cls,
        passages: Iterable[Passage],
        db_path: str = ":memory:",
        *,
        w_fts: float = 0.5,
        w_sem: float = 0.5,
    ) -> HybridRetriever:
        """Construye un HybridRetriever indexado a partir de pasajes en memoria."""
        retriever = cls(db_path, w_fts=w_fts, w_sem=w_sem)
        retriever.add_passages(passages)
        return retriever

    @classmethod
    def from_json(cls, ruta_json: Path | str, db_path: str = ":memory:") -> HybridRetriever:
        """Carga e indexa pasajes desde un archivo JSON."""
        path = Path(ruta_json)
        with path.open(encoding="utf-8") as f:
            datos = json.load(f)
        passages = [
            Passage(
                document=d["document"],
                section=d["section"],
                version=d.get("version", "Actualización 2024"),
                text=d["text"],
                score=0.0,
                is_example=d.get("is_example", False),
            )
            for d in datos
        ]
        return cls.from_passages(passages, db_path)

    @classmethod
    def default(cls) -> HybridRetriever:
        """Carga el corpus oficial por defecto (data/emre_passages.json)."""
        path = _DEFAULT_PASSAGES_JSON
        if not path.is_file():
            path = Path("data/emre_passages.json")
        if path.is_file():
            return cls.from_json(path)
        return cls(":memory:")

    def close(self) -> None:
        """Cierra la conexión SQLite."""
        self._con.close()
