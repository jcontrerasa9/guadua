"""Esquema de eventos e interfaz de almacenamiento. CONTRATO: ver docs/diseno.md §9."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

type EventId = tuple[str, int]
"""(nodo_origen, consecutivo): identifica un evento en todo el sistema."""

type VersionVector = dict[str, int]
"""Por cada nodo de origen, el último consecutivo que se tiene completo."""


class EventCategory(StrEnum):
    HECHO = "hecho"  # reporte, SOS, a salvo, transcripción; lo crea quien lo recibe
    DERIVADO = "derivado"  # urgencia, duplicados, ficha; solo lo crea el nodo dueño
    DECISION = "decision"  # aprobar, editar, rechazar; viene del tablero


class Event(BaseModel):
    """Evento inmutable. Nunca se edita ni se borra: los cambios son eventos nuevos."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin_node: str = Field(min_length=1)
    seq: int = Field(ge=1)
    category: EventCategory
    type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    refs: tuple[EventId, ...] = ()  # eventos en los que se basa (p. ej., el reporte original)
    received_at: float  # hora de recepción en el nodo (Unix, s); mide antigüedad, no ordena

    @property
    def id(self) -> EventId:
        return (self.origin_node, self.seq)


@runtime_checkable
class EventStore(Protocol):
    """Registro de eventos de un nodo. Solo agrega; recibir un repetido no hace daño."""

    def append(self, event: Event) -> bool:
        """Guarda el evento. Devuelve False si ya existía (mismo id)."""
        ...

    def get(self, event_id: EventId) -> Event | None: ...

    def since(self, vector: VersionVector) -> list[Event]:
        """Eventos que el dueño de `vector` todavía no tiene."""
        ...

    def version_vector(self) -> VersionVector: ...

    def digest(self) -> str:
        """SHA-256 del conjunto ordenado de eventos (verificación de convergencia)."""
        ...
