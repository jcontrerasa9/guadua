"""Comando del banco de fallos: `python -m guadua.bench [--semilla N] [--rapido] [--salida F]`.

La tabla va a la salida estándar (o a `--salida`); el progreso, a la salida de errores, para
que lo que se pega en el documento de resultados quede limpio.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from guadua.bench.escenarios import (
    channels,
    fits,
    message_of,
    run_failover,
    run_partition,
    run_replication,
    run_send,
    sample_reports,
)
from guadua.bench.tabla import Results, Static, render
from guadua.channel.profiles import LORA, SMS, WIFI, ChannelProfile
from guadua.classifier.evaluate import evaluate
from guadua.classifier.synthetic import Row, load
from guadua.classifier.triage import default_classifier
from guadua.protocol.header import Flag, Header, Incident, MessageType, People, encode
from guadua.protocol.text_savings import load_corpus, measure

DEFAULT_SEED = 15
SMS_BYTES = 140


@dataclass(frozen=True, slots=True)
class Config:
    profiles: tuple[ChannelProfile, ...]
    losses: tuple[float, ...]
    cuts: bool  # agregar la fila del perfil tal cual (con cortes)
    send_reports: int
    replication_reports: int
    partition_reports: int  # por lado


FULL = Config((WIFI, LORA, SMS), (0.0, 0.3, 0.6), True, 60, 20, 10)
QUICK = Config((LORA,), (0.0, 0.6), False, 20, 8, 4)


def static_metrics(rows: list[Row], classifier) -> Static:
    sos = Header(MessageType.SOS_ESCOMBROS, 1, 1, 3600, Incident.ATRAPADOS, People.NO_SE,
                 Flag.ATRAPADOS, barrio=0)  # fmt: skip
    test = [r for r in rows if r.split == "test"]
    sizes = [len(message_of(r, 1, i)) for i, r in enumerate(test, 1) if fits(r)]
    return Static(
        sos_bytes=len(encode(sos)),
        report_count=len(sizes),
        too_long=len(test) - len(sizes),
        mean_bytes=sum(sizes) / len(sizes),
        max_bytes=max(sizes),
        under_sms=sum(s <= SMS_BYTES for s in sizes) / len(sizes),
        dictionary_savings=measure(load_corpus()).savings,
        evaluation=evaluate(rows, classifier),
    )


async def run(
    seed: int = DEFAULT_SEED,
    config: Config = FULL,
    *,
    command: str = "python -m guadua.bench",
    progress: Callable[[str], None] = lambda _: None,
) -> Results:
    rows = load()
    classifier = default_classifier()
    progress("métricas sin canal: bytes, diccionario y clasificador")
    static = static_metrics(rows, classifier)

    send_samples = sample_reports(rows, classifier, config.send_reports, seed)
    repl_samples = sample_reports(rows, classifier, config.replication_reports, seed)
    side_a = sample_reports(rows, classifier, config.partition_reports, seed, device_id=0xAA000000)
    side_c = sample_reports(rows, classifier, config.partition_reports, seed, device_id=0xCC000000)
    fail_samples = sample_reports(rows, classifier, 2, seed, device_id=0xF0000000)

    res = Results(seed, command, static, [], [], [], [])
    rows_of_channels = channels(config.profiles, config.losses, cuts=config.cuts)
    for i, channel in enumerate(rows_of_channels, 1):
        started = time.perf_counter()
        res.send.append(await run_send(channel, send_samples, seed, classifier))
        res.replication.append(await run_replication(channel, repl_samples, seed, classifier))
        res.partition.append(await run_partition(channel, side_a, side_c, seed, classifier))
        with tempfile.TemporaryDirectory() as tmp:
            res.failover.append(
                await run_failover(channel, fail_samples, seed, classifier, Path(tmp))
            )
        took = time.perf_counter() - started
        progress(f"[{i}/{len(rows_of_channels)}] {channel.name} {channel.label}: {took:.1f} s")
    return res


@contextlib.contextmanager
def quiet_logs() -> Iterator[None]:
    """Los nodos anuncian tomas y recuperaciones en el log (como warning); en el banco solo
    estorban: lo que miden ya sale en la tabla."""
    logger = logging.getLogger("guadua")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(level)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m guadua.bench", description="Banco de fallos con métricas (N15)."
    )
    parser.add_argument("--semilla", type=int, default=DEFAULT_SEED, help="semilla de la red")
    parser.add_argument("--rapido", action="store_true", help="solo LoRa 0 y 60 %%, menos reportes")
    parser.add_argument("--salida", type=Path, help="escribe la tabla en este archivo Markdown")
    args = parser.parse_args(argv)

    command = "python -m guadua.bench" + (f" --semilla {args.semilla}")
    command += " --rapido" if args.rapido else ""

    def progress(text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    started = time.perf_counter()
    with quiet_logs():
        results = asyncio.run(
            run(args.semilla, QUICK if args.rapido else FULL, command=command, progress=progress)
        )
    table = render(results)
    if args.salida:
        args.salida.write_text(table, encoding="utf-8")
        progress(f"tabla escrita en {args.salida}")
    else:
        print(table)
    progress(f"listo en {time.perf_counter() - started:.1f} s")
