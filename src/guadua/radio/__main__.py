"""Herramientas de la radio VHF (D7).

    python -m guadua.radio descargar-modelo small        # una sola vez, con Internet (~480 MB)
    python -m guadua.radio transcribir data/radio/clip.ogg
    python -m guadua.radio enviar data/radio/clip.ogg --nodo http://localhost:8000
    python -m guadua.radio preparar grabacion.m4a data/radio/clip_x.ogg   # sonido de radio

`descargar-modelo` deja el modelo en data/models/whisper-<modelo> (o en --dir): el nodo lo
lee de ahí sin red. `preparar` usa ffmpeg: filtro de banda de radio (300–3000 Hz), ruido
suave y Opus mono de 16 kHz.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from guadua.providers.transcriber import (
    TranscriberUnavailable,
    model_dir,
    transcriber_from_env,
)
from guadua.radio.clips import EXTENSIONS

MODEL_FILES = [
    "config.json", "model.bin", "tokenizer.json", "vocabulary.*", "preprocessor_config.json",
]  # fmt: skip
VHF_FILTER = (
    "[0:a]highpass=f=300,lowpass=f=3000,acompressor=threshold=0.1:ratio=4,"
    "aresample=16000,aformat=channel_layouts=mono[voz];"
    "anoisesrc=color=pink:amplitude=0.02:sample_rate=16000[ruido];"
    "[voz][ruido]amix=inputs=2:duration=first:normalize=0[radio]"
)


def descargar(model: str, folder: Path | None) -> None:
    from huggingface_hub import snapshot_download

    target = folder or model_dir(model)
    print(f"Descargando Systran/faster-whisper-{model} en {target}")
    print("small ≈ 480 MB (1–3 min con buena red) · base ≈ 145 MB (< 1 min)", flush=True)
    started = time.monotonic()
    snapshot_download(
        f"Systran/faster-whisper-{model}", local_dir=target, allow_patterns=MODEL_FILES
    )
    size = sum(p.stat().st_size for p in target.rglob("*") if p.is_file()) / 1e6
    print(f"Listo en {time.monotonic() - started:.0f} s · {size:.0f} MB en {target}")


def transcribir(paths: list[Path]) -> None:
    transcriber = transcriber_from_env()
    print(f"Transcriptor: {transcriber.name}", flush=True)
    for path in paths:
        started = time.monotonic()
        try:
            t = transcriber.transcribe(path.read_bytes())
        except TranscriberUnavailable as exc:
            print(f"✗ {path.name}: {exc}")
            continue
        took = time.monotonic() - started
        print(f"\n{path.name} · {t.duration_s:.1f} s de audio en {took:.1f} s "
              f"(confianza {t.confidence})\n  {t.text}", flush=True)  # fmt: skip


def enviar(path: Path, node: str) -> None:
    import httpx

    kind = EXTENSIONS.get(path.suffix.lower())
    if kind is None:
        sys.exit(f"extensión no soportada: {path.suffix} (sirven {', '.join(EXTENSIONS)})")
    started = time.monotonic()
    res = httpx.post(
        f"{node.rstrip('/')}/api/radio",
        content=path.read_bytes(),
        headers={"Content-Type": kind},
        timeout=300,
    )
    print(f"{res.status_code} en {time.monotonic() - started:.1f} s")
    print(json.dumps(res.json(), ensure_ascii=False, indent=2))


def preparar(source: Path, target: Path) -> None:
    if shutil.which("ffmpeg") is None:
        sys.exit("falta ffmpeg (sudo apt install ffmpeg)")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-filter_complex", VHF_FILTER,
         "-map", "[radio]", "-c:a", "libopus", "-b:a", "24k", str(target)],
        check=True,
    )  # fmt: skip
    print(f"{target} · {target.stat().st_size / 1024:.0f} KB")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m guadua.radio", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("descargar-modelo", help="baja el modelo de Whisper (una vez)")
    p.add_argument("modelo", nargs="?", default="small", choices=["tiny", "base", "small"])
    p.add_argument("--dir", type=Path, default=None)
    p = sub.add_parser("transcribir", help="transcribe clips aquí mismo, con tiempos")
    p.add_argument("archivos", nargs="+", type=Path)
    p = sub.add_parser("enviar", help="manda un clip a un nodo (POST /api/radio)")
    p.add_argument("archivo", type=Path)
    p.add_argument("--nodo", default="http://localhost:8000")
    p = sub.add_parser("preparar", help="da sonido de radio VHF a una grabación")
    p.add_argument("entrada", type=Path)
    p.add_argument("salida", type=Path)
    args = parser.parse_args(argv)
    match args.cmd:
        case "descargar-modelo":
            descargar(args.modelo, args.dir)
        case "transcribir":
            transcribir(args.archivos)
        case "enviar":
            enviar(args.archivo, args.nodo)
        case "preparar":
            preparar(args.entrada, args.salida)


if __name__ == "__main__":
    main()
