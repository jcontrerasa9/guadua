"""Interfaz del RAG. CONTRATO: ver docs/diseno.md §13."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Passage:
    document: str
    section: str
    version: str
    text: str
    score: float
    is_example: bool = False  # True si no viene de un documento oficial


@runtime_checkable
class Retriever(Protocol):
    def search(self, query: str, k: int = 3) -> list[Passage]:
        """Pasajes ordenados de mayor a menor relevancia, con su cita."""
        ...
