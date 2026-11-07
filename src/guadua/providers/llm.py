"""Interfaz del proveedor de LLM (Gemini, Ollama o simulado). CONTRATO."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


class LLMUnavailable(Exception):
    """Sin LLM o respuesta inválida: el caso pasa a "ambiguo, revisar"."""


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass(frozen=True, slots=True)
class LLMReply:
    """O pide una herramienta (tool_name + tool_args) o termina con un JSON final."""

    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    final: dict[str, Any] | None = None


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> LLMReply:
        """Un paso del ciclo de herramientas. Lanza LLMUnavailable si no hay respuesta válida."""
        ...
