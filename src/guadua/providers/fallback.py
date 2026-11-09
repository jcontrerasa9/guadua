"""Cadena de LLM: primero el de la nube, si falla el local, si falla ninguno (diseño §4 y §14).

    Gemini (con Internet) → Ollama (sin Internet) → LLMUnavailable → "ambiguo, revisar"

`llm_from_env` arma la cadena con las variables de `.env.example`, leídas del **entorno**
(el código nunca abre `.env`, regla 8 de AGENTS.md):

    LLM_PROVIDER    auto | gemini | ollama | none      (auto si no está)
    GEMINI_API_KEY  y GEMINI_MODEL                     sin las dos, no se usa Gemini
    OLLAMA_HOST     http://localhost:11434 por defecto
    OLLAMA_MODEL                                       sin él, no se usa Ollama

No se elige ningún modelo por defecto: siguen abiertos en el diseño (§18). Sin nada
configurado no hay LLM y el nodo funciona igual, con los casos dudosos en revisión humana.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence

from guadua.providers.http_llm import GeminiLLM, OllamaLLM
from guadua.providers.llm import LLMProvider, LLMReply, LLMUnavailable, Message, ToolSpec

logger = logging.getLogger("guadua.llm")

COOLDOWN_S = 60.0  # tras una falla, el proveedor se salta un rato: sin Internet no se espera
PROVIDERS = ("auto", "gemini", "ollama", "none")


class LLMConfigError(ValueError):
    """LLM_PROVIDER pide un proveedor que no está configurado."""


class FallbackLLM:
    """Prueba los proveedores en orden. `last_used` dice cuál respondió la última vez."""

    def __init__(
        self,
        providers: Sequence[LLMProvider],
        *,
        cooldown_s: float = COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not providers:
            raise ValueError("la cadena necesita al menos un proveedor")
        self.providers = tuple(providers)
        self.name = "+".join(p.name for p in self.providers)
        self.last_used: str | None = None
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._failed_at: dict[int, float] = {}

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> LLMReply:
        errors = []
        now = self._clock()
        for index, provider in enumerate(self.providers):
            failed_at = self._failed_at.get(index)
            if failed_at is not None and now - failed_at < self._cooldown_s:
                errors.append(f"{provider.name}: en espera tras una falla")
                continue
            try:
                reply = provider.complete(messages, tools)
            except LLMUnavailable as exc:
                self._failed_at[index] = self._clock()
                logger.warning("falló %s; se prueba el siguiente: %s", provider.name, exc)
                errors.append(str(exc))
                continue
            self._failed_at.pop(index, None)
            self.last_used = _label(provider)
            return reply
        self.last_used = None
        raise LLMUnavailable("ningún LLM respondió: " + " · ".join(errors))


def _label(provider: LLMProvider) -> str:
    model = getattr(provider, "model", None)
    return f"{provider.name}/{model}" if model else provider.name


def llm_from_env(env: Mapping[str, str] | None = None) -> FallbackLLM | None:
    """La cadena configurada, o None si no hay ningún LLM (el caso normal en Docker)."""
    env = os.environ if env is None else env
    choice = (env.get("LLM_PROVIDER") or "auto").strip().lower()
    if choice not in PROVIDERS:
        raise LLMConfigError(f"LLM_PROVIDER inválido: {choice!r} (opciones: {PROVIDERS})")
    if choice == "none":
        return None
    key, gemini_model = env.get("GEMINI_API_KEY", ""), env.get("GEMINI_MODEL", "")
    ollama_model = env.get("OLLAMA_MODEL", "")
    host = env.get("OLLAMA_HOST") or "http://localhost:11434"
    providers: list[LLMProvider] = []
    if choice in ("auto", "gemini") and key and gemini_model:
        providers.append(GeminiLLM(key, gemini_model))
    elif choice == "gemini":
        raise LLMConfigError("LLM_PROVIDER=gemini necesita GEMINI_API_KEY y GEMINI_MODEL")
    if choice in ("auto", "ollama") and ollama_model:
        providers.append(OllamaLLM(ollama_model, host=host))
    elif choice == "ollama":
        raise LLMConfigError("LLM_PROVIDER=ollama necesita OLLAMA_MODEL")
    return FallbackLLM(providers) if providers else None
