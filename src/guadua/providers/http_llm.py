"""Clientes de LLM por HTTP: Gemini (nube) y Ollama (local). Implementan `LLMProvider`.

Van directo sobre `httpx` (ya es dependencia): sin SDK ni descargas nuevas. Los dos usan el
protocolo de providers/json_protocol.py con salida forzada a JSON y temperatura 0.
Cualquier falla (red, tiempo, 4xx/5xx, JSON inválido) se vuelve LLMUnavailable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from guadua.providers.json_protocol import TOOL_RESULT_PREFIX, parse_reply, tools_prompt
from guadua.providers.llm import LLMReply, LLMUnavailable, Message, ToolSpec

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_TIMEOUT_S = 20.0
OLLAMA_TIMEOUT_S = 90.0  # en CPU un modelo pequeño puede tardar


def _system_text(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> str:
    parts = [m.content for m in messages if m.role == "system"]
    return "\n\n".join([*parts, tools_prompt(tools)])


def _turns(messages: Sequence[Message]) -> list[tuple[str, str]]:
    """(rol "user"|"assistant", texto) sin el sistema; el resultado de una herramienta va
    como mensaje del usuario, que todos los modelos entienden."""
    turns = []
    for m in messages:
        if m.role == "assistant":
            turns.append(("assistant", m.content))
        elif m.role == "tool":
            turns.append(("user", TOOL_RESULT_PREFIX + m.content))
        elif m.role == "user":
            turns.append(("user", m.content))
    return turns


class _HttpLLM:
    name = "http"

    def __init__(self, model: str, client: httpx.Client) -> None:
        if not model:
            raise ValueError("falta el modelo")
        self.model = model
        self._client = client

    def _post(self, url: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.post(url, json=body, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMUnavailable(f"{self.name}: {type(exc).__name__}: {exc}"[:200]) from exc


class GeminiLLM(_HttpLLM):
    """Gemini por la API REST `generateContent`. Necesita Internet y una API key."""

    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = GEMINI_URL,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("falta GEMINI_API_KEY")
        super().__init__(model, client or httpx.Client(timeout=GEMINI_TIMEOUT_S))
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> LLMReply:
        body = {
            "systemInstruction": {"parts": [{"text": _system_text(messages, tools)}]},
            "contents": [
                {"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]}
                for role, text in _turns(messages)
            ],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
        }
        url = f"{self._base_url}/models/{self.model}:generateContent"
        data = self._post(url, body, headers={"x-goog-api-key": self._api_key})
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable("gemini: respuesta sin candidatos") from exc
        return parse_reply(text)


class OllamaLLM(_HttpLLM):
    """Ollama local por `/api/chat`. Funciona sin Internet."""

    name = "ollama"

    def __init__(
        self,
        model: str,
        *,
        host: str = "http://localhost:11434",
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(model, client or httpx.Client(timeout=OLLAMA_TIMEOUT_S))
        self._host = host.rstrip("/")

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> LLMReply:
        chat = [{"role": "system", "content": _system_text(messages, tools)}]
        chat += [{"role": role, "content": text} for role, text in _turns(messages)]
        body = {
            "model": self.model,
            "messages": chat,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0},
        }
        data = self._post(f"{self._host}/api/chat", body)
        try:
            text = data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMUnavailable("ollama: respuesta sin mensaje") from exc
        return parse_reply(text)
