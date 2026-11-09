"""Protocolo de herramientas en JSON simple, igual para Gemini y Ollama.

No se usa el "function calling" de cada proveedor: los modelos pequeños de Ollama lo usan mal
(diseño §10). Las herramientas se describen en un mensaje de sistema y el modelo responde
**solo** un objeto JSON, con salida forzada a JSON en ambos proveedores:

    {"tool": "<nombre>", "args": {...}}     pide una herramienta
    {"final": {...}}                        termina con su respuesta

Cualquier otra cosa es una respuesta inválida → LLMUnavailable → "ambiguo, revisar".
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from guadua.providers.llm import LLMReply, LLMUnavailable, ToolSpec

TOOL_RESULT_PREFIX = "Resultado de la herramienta:\n"


def tools_prompt(tools: Sequence[ToolSpec]) -> str:
    """Texto que se agrega al mensaje de sistema con las herramientas permitidas."""
    lines = [
        "Responde SOLO con un objeto JSON, sin texto alrededor.",
        'Para usar una herramienta: {"tool": "<nombre>", "args": {...}}.',
        'Para terminar: {"final": {...}}.',
        "Herramientas permitidas (no existen otras):",
    ]
    for tool in tools:
        schema = json.dumps(tool.parameters, ensure_ascii=False)
        lines.append(f"- {tool.name}: {tool.description} Argumentos (JSON Schema): {schema}")
    return "\n".join(lines)


def parse_reply(text: str) -> LLMReply:
    """Texto del modelo → LLMReply. Lanza LLMUnavailable si no cumple el protocolo."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise LLMUnavailable(f"el modelo no devolvió JSON: {str(text)[:80]!r}") from exc
    if not isinstance(data, dict):
        raise LLMUnavailable("el modelo no devolvió un objeto JSON")
    if isinstance(data.get("final"), dict) and "tool" not in data:
        return LLMReply(final=data["final"])
    tool, args = data.get("tool"), data.get("args", {})
    if isinstance(tool, str) and tool and isinstance(args, dict) and "final" not in data:
        return LLMReply(tool_name=tool, tool_args=args)
    raise LLMUnavailable(f"respuesta fuera del protocolo: {str(text)[:80]!r}")
