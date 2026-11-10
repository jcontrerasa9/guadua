"""Interfaz del transcriptor de radio (faster-whisper, ninguno o simulado). CONTRATO (D7).

El nodo que recibe un clip de radio lo pasa por un `Transcriber`. Si no hay transcriptor o
falla, el clip queda "pendiente" y el operador escribe el texto en el tablero (diseño §7):
como sin LLM, el caso va a una persona. Las pruebas usan uno simulado (tests/fakes.py).

`transcriber_from_env` lee del **entorno** (nunca de `.env`, regla 8):

    SARD_TRANSCRIBER     auto | whisper | none                      auto
    WHISPER_MODEL        tamaño del modelo: base, small…            small
    WHISPER_MODEL_DIR    carpeta del modelo ya descargado           data/models/whisper-<modelo>
    WHISPER_THREADS      hilos de CPU para transcribir              2

`auto` usa faster-whisper solo si está instalado (extra `radio`) y el modelo ya está en
disco; nunca descarga nada al arrancar (`python -m guadua.radio descargar-modelo`).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger("guadua.radio")

ROOT = Path(__file__).resolve().parents[3]
MODELS_DIR = ROOT / "data" / "models"
DEFAULT_MODEL = "small"
DEFAULT_THREADS = 2
CHOICES = ("auto", "whisper", "none")


class TranscriberUnavailable(Exception):
    """No hay transcriptor o no pudo con el audio: el clip queda pendiente para el operador."""


@dataclass(frozen=True, slots=True)
class Transcription:
    text: str
    language: str
    duration_s: float
    confidence: float | None  # probabilidad media de las palabras (0 a 1); None si no se sabe
    model: str  # p. ej. "faster-whisper/small-int8"


@runtime_checkable
class Transcriber(Protocol):
    name: str

    def transcribe(self, audio: bytes) -> Transcription:
        """Audio (wav, ogg, mp3, webm…) → texto en español. Bloquea: el nodo lo corre en otro
        hilo. Lanza TranscriberUnavailable si no puede."""
        ...


class NullTranscriber:
    """Sin transcriptor: todo clip queda pendiente para el operador."""

    name = "ninguno"

    def transcribe(self, audio: bytes) -> Transcription:
        raise TranscriberUnavailable("no hay transcriptor: el operador escribe el texto")


class TranscriberConfigError(ValueError):
    """SARD_TRANSCRIBER pide algo que no está instalado o configurado."""


def model_dir(model: str, env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("WHISPER_MODEL_DIR") or MODELS_DIR / f"whisper-{model}")


def transcriber_from_env(env: Mapping[str, str] | None = None) -> Transcriber:
    env = os.environ if env is None else env
    choice = (env.get("SARD_TRANSCRIBER") or "auto").strip().lower()
    if choice not in CHOICES:
        raise TranscriberConfigError(f"SARD_TRANSCRIBER inválido: {choice!r} (opciones: {CHOICES})")
    if choice == "none":
        return NullTranscriber()
    model = env.get("WHISPER_MODEL") or DEFAULT_MODEL
    folder = model_dir(model, env)
    try:
        threads = int(env.get("WHISPER_THREADS") or DEFAULT_THREADS)
    except ValueError as exc:
        raise TranscriberConfigError(
            f"WHISPER_THREADS inválido: {env['WHISPER_THREADS']!r}"
        ) from exc
    try:
        from guadua.radio.whisper import FasterWhisperTranscriber
    except ImportError as exc:
        if choice == "whisper":
            raise TranscriberConfigError("falta faster-whisper: pip install -e '.[radio]'") from exc
        return NullTranscriber()
    if not (folder / "model.bin").is_file():
        if choice == "whisper":
            raise TranscriberConfigError(
                f"no está el modelo en {folder}: python -m guadua.radio descargar-modelo {model}"
            )
        logger.warning("sin modelo de Whisper en %s: los clips quedan para el operador", folder)
        return NullTranscriber()
    return FasterWhisperTranscriber(folder, model=model, threads=threads)
