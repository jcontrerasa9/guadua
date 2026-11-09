"""Mide el ahorro del diccionario frente a no comprimir (métrica de docs/diseno.md §15).

Uso: python -m guadua.protocol.text_savings [archivo con una frase por línea]
"""

from __future__ import annotations

import sys
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from guadua.protocol.header import HEADER_SIZE
from guadua.protocol.text import encode_text

DEFAULT_CORPUS = Path(__file__).resolve().parents[3] / "data" / "frases_prueba_texto.txt"


@dataclass(frozen=True, slots=True)
class Row:
    text: str
    plain: int  # UTF-8 sin comprimir
    zlib_plain: int  # zlib normal, sin diccionario (referencia)
    dictionary: int  # formato del contrato: prefijo + deflate con diccionario


@dataclass(frozen=True, slots=True)
class Summary:
    rows: tuple[Row, ...]

    @property
    def total_plain(self) -> int:
        return sum(r.plain for r in self.rows)

    @property
    def total_dictionary(self) -> int:
        return sum(r.dictionary for r in self.rows)

    @property
    def total_zlib_plain(self) -> int:
        return sum(r.zlib_plain for r in self.rows)

    @property
    def savings(self) -> float:
        """Fracción ahorrada frente a no comprimir (0.5 = la mitad)."""
        return 1 - self.total_dictionary / self.total_plain

    @property
    def mean_message(self) -> float:
        """Bytes promedio del mensaje completo: cabecera + texto."""
        return HEADER_SIZE + self.total_dictionary / len(self.rows)


def load_corpus(path: Path = DEFAULT_CORPUS) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def measure(texts: Iterable[str]) -> Summary:
    rows = []
    for text in texts:
        raw = text.encode("utf-8")
        rows.append(Row(text, len(raw), len(zlib.compress(raw, 9)), len(encode_text(text))))
    if not rows:
        raise ValueError("el corpus está vacío")
    return Summary(tuple(rows))


def format_table(summary: Summary) -> str:
    lines = [f"{'plano':>5} {'zlib':>5} {'dicc.':>5}  frase", "-" * 60]
    for r in summary.rows:
        lines.append(f"{r.plain:>5} {r.zlib_plain:>5} {r.dictionary:>5}  {r.text}")
    lines += [
        "-" * 60,
        f"{summary.total_plain:>5} {summary.total_zlib_plain:>5} "
        f"{summary.total_dictionary:>5}  total ({len(summary.rows)} frases)",
        f"Ahorro con diccionario frente a no comprimir: {summary.savings:.1%}",
        f"Mensaje promedio (cabecera {HEADER_SIZE} B + texto): {summary.mean_message:.1f} B",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    corpus = load_corpus(Path(args[0])) if args else load_corpus()
    print(format_table(measure(corpus)))


if __name__ == "__main__":
    main()
