"""Dónde guarda el nodo el audio de radio (D7). Interfaz `ClipStore` (regla 4).

El audio no viaja por gossip (las tramas son de 200 B): queda en el nodo que lo recibió,
para reproducirlo en el tablero. Lo que viaja es la transcripción, como evento.

- `MemoryClipStore`: en memoria (pruebas, nodo con `SARD_DB=:memory:`).
- `DirClipStore`: una carpeta junto a la SQLite del nodo (`<id>.audio` + `<id>.json`).

El id del clip son los primeros 16 hex del SHA-256 del audio: el mismo clip enviado dos
veces es el mismo id, y el nodo responde `duplicate: true`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from guadua.store.event_types import AUDIO_ID_PATTERN

MAX_AUDIO_BYTES = 8 * 1024 * 1024  # ~2 min en WAV de 16 kHz; mucho más en Opus
CONTENT_TYPES = {
    "audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave", "audio/ogg", "audio/opus",
    "audio/mpeg", "audio/mp3", "audio/mp4", "audio/x-m4a", "audio/aac", "audio/webm",
    "audio/flac", "audio/x-flac", "audio/m4a", "audio/x-opus+ogg", "audio/3gpp", "audio/amr",
    # Lo que algunos navegadores y celulares ponen a un audio (notas de voz de WhatsApp,
    # grabaciones del navegador): se guarda como el tipo de audio equivalente.
    "application/ogg", "video/ogg", "video/webm", "video/mp4",
}  # fmt: skip
SAME_AS = {
    "application/ogg": "audio/ogg", "video/ogg": "audio/ogg", "audio/x-opus+ogg": "audio/ogg",
    "video/webm": "audio/webm", "video/mp4": "audio/mp4", "audio/m4a": "audio/mp4",
}  # fmt: skip
EXTENSIONS = {
    ".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4", ".webm": "audio/webm", ".flac": "audio/flac", ".aac": "audio/aac",
    ".3gp": "audio/3gpp", ".amr": "audio/amr",
}  # fmt: skip
_AUDIO_ID = re.compile(AUDIO_ID_PATTERN)


class ClipError(ValueError):
    """Audio inválido → 400."""


def audio_id_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def check_audio(data: bytes, content_type: str | None) -> str:
    """El tipo de contenido normalizado, o ClipError."""
    kind = (content_type or "").split(";")[0].strip().lower()
    if kind not in CONTENT_TYPES:
        raise ClipError(f"tipo de audio no soportado: {kind or 'ninguno'!r}")
    if not data:
        raise ClipError("audio vacío")
    if len(data) > MAX_AUDIO_BYTES:
        raise ClipError(f"audio de más de {MAX_AUDIO_BYTES // (1024 * 1024)} MB")
    return SAME_AS.get(kind, kind)


@dataclass(frozen=True, slots=True)
class Clip:
    audio_id: str
    content_type: str
    received_at: float
    size: int
    name: str | None = None  # nombre del archivo o del clip de demostración


class ClipStore(Protocol):
    def put(self, clip: Clip, data: bytes) -> bool:
        """Guarda el clip. False si ya estaba (no lo reemplaza)."""
        ...

    def get(self, audio_id: str) -> tuple[Clip, bytes] | None: ...

    def clips(self) -> list[Clip]:
        """Todos, del más antiguo al más reciente."""
        ...


class MemoryClipStore:
    def __init__(self) -> None:
        self._clips: dict[str, tuple[Clip, bytes]] = {}

    def put(self, clip: Clip, data: bytes) -> bool:
        if clip.audio_id in self._clips:
            return False
        self._clips[clip.audio_id] = (clip, data)
        return True

    def get(self, audio_id: str) -> tuple[Clip, bytes] | None:
        return self._clips.get(audio_id)

    def clips(self) -> list[Clip]:
        return sorted((c for c, _ in self._clips.values()), key=lambda c: c.received_at)


class DirClipStore:
    def __init__(self, folder: str | Path) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def _paths(self, audio_id: str) -> tuple[Path, Path]:
        if not _AUDIO_ID.match(audio_id):
            raise ClipError(f"id de clip inválido: {audio_id!r}")
        return self.folder / f"{audio_id}.audio", self.folder / f"{audio_id}.json"

    def put(self, clip: Clip, data: bytes) -> bool:
        audio, meta = self._paths(clip.audio_id)
        if meta.exists():
            return False
        audio.write_bytes(data)
        meta.write_text(json.dumps(asdict(clip), ensure_ascii=False), "utf-8")  # al final
        return True

    def get(self, audio_id: str) -> tuple[Clip, bytes] | None:
        try:
            audio, meta = self._paths(audio_id)
            return Clip(**json.loads(meta.read_text("utf-8"))), audio.read_bytes()
        except (ClipError, OSError, ValueError, TypeError):
            return None

    def clips(self) -> list[Clip]:
        out = []
        for meta in self.folder.glob("*.json"):
            try:
                out.append(Clip(**json.loads(meta.read_text("utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
        return sorted(out, key=lambda c: c.received_at)


def clip_store_for(db_path: str) -> ClipStore:
    """Junto a la SQLite del nodo; en memoria si la base es `:memory:`."""
    if db_path == ":memory:":
        return MemoryClipStore()
    return DirClipStore(Path(db_path).resolve().parent / "audio")
