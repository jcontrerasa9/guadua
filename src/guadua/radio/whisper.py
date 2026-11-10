"""Transcriptor con faster-whisper en CPU (D7, diseño §4 y §7). Implementa `Transcriber`.

- **Modelo cuantizado (int8):** pesos de 8 bits en lugar de 32; ocupa ~4 veces menos y va
  más rápido en CPU con muy poca pérdida.
- **Solo archivos locales:** `local_files_only=True`; el modelo se bajó antes, a una carpeta
  del proyecto o de la imagen de Docker. Sin red nunca intenta descargar.
- **Se carga al primer clip** (unos segundos), una vez por proceso, y solo en el nodo que
  recibe audio: la transcripción viaja a los demás como evento.
- **Español y vocabulario de Manizales:** `language="es"` y un `initial_prompt` con los
  barrios y la jerga de radio, que ayuda a escribir bien los nombres de lugar.
- **VAD:** `vad_filter` salta los silencios y el ruido del canal entre frases.
"""

from __future__ import annotations

import io
import json
import logging
import math
import threading
from pathlib import Path

from guadua.providers.transcriber import ROOT, TranscriberUnavailable, Transcription

logger = logging.getLogger("guadua.radio")

BARRIOS_PATH = ROOT / "data" / "barrios.json"
BEAM_SIZE = 5


def vocabulary_prompt() -> str:
    try:
        names = [b["name"] for b in json.loads(BARRIOS_PATH.read_text("utf-8"))]
    except (OSError, ValueError, KeyError):
        names = []
    barrios = ", ".join(names)
    return (
        "Transmisión de radio de Bomberos de Manizales. "
        f"Barrios: {barrios}. Incendio, colapso, personas atrapadas, fuga de gas, heridos, "
        "vía bloqueada. Estamos en el sitio. Nos comentan que. QTH. Cambio."
    )


class FasterWhisperTranscriber:
    def __init__(self, folder: Path, *, model: str, threads: int = 2) -> None:
        self.folder = Path(folder)
        self.model_name = model
        self.threads = threads
        self.name = f"faster-whisper/{model}-int8"
        self._model = None
        self._lock = threading.Lock()  # un clip a la vez: no le quita CPU a los nodos

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            logger.info("cargando Whisper %s desde %s", self.model_name, self.folder)
            self._model = WhisperModel(
                str(self.folder),
                device="cpu",
                compute_type="int8",
                cpu_threads=self.threads,
                local_files_only=True,
            )
        return self._model

    def transcribe(self, audio: bytes) -> Transcription:
        with self._lock:
            try:
                model = self._load()
                segments, info = model.transcribe(
                    io.BytesIO(audio),
                    language="es",
                    beam_size=BEAM_SIZE,
                    vad_filter=True,
                    initial_prompt=vocabulary_prompt(),
                    condition_on_previous_text=False,
                )
                parts = list(segments)  # la transcripción ocurre al recorrer el generador
            except Exception as exc:  # audio dañado, formato raro, modelo roto
                raise TranscriberUnavailable(f"Whisper no pudo con el audio: {exc}") from exc
        text = " ".join(" ".join(s.text.split()) for s in parts).strip()
        chars = sum(len(s.text) for s in parts)
        confidence = (
            math.exp(sum(s.avg_logprob * len(s.text) for s in parts) / chars) if chars else None
        )
        return Transcription(
            text=text,
            language=info.language,
            duration_s=round(float(info.duration), 2),
            confidence=round(min(max(confidence, 0.0), 1.0), 3) if confidence is not None else None,
            model=self.name,
        )
