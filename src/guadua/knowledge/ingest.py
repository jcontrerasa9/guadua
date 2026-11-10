"""Corpus del RAG desde los PDF oficiales de data/fuentes (D8). docs/diseno.md §13.

    python -m guadua.knowledge.ingest          # reescribe data/emre_passages.json

Cada página de cada PDF se parte en pasajes de ~60 a 180 palabras con el texto **tal como
está en el documento**, y la cita es el documento y la página ("p. 23"): quien lea la ficha
puede abrir el PDF y encontrarlo. Las páginas de tablas (matrices R/A/L) y de índice no
sirven como texto y se descartan.

Los resúmenes operativos que escribió el equipo (data/rag_resumenes_equipo.json) se agregan
marcados `is_example=True`: aportan lo táctico que la EMRE no trae, pero el tablero los
muestra como "(ejemplo)", nunca como norma oficial.

Necesita `pdftotext` (poppler-utils) solo para regenerar el corpus; el nodo lee el JSON ya
generado y funciona sin él.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parents[3] / "data"
FUENTES = DATA / "fuentes"
SALIDA = DATA / "emre_passages.json"
RESUMENES = DATA / "rag_resumenes_equipo.json"
VERSION = "Actualización 2024"

MIN_PALABRAS = 60
MAX_PALABRAS = 180
MAX_PALABRAS_TITULO = 8
MAX_CELDAS = 0.12  # páginas de matrices: "R A L R A A 14 15 16 …"
_CELDA = re.compile(r"^(?:[RAL]|\d{1,3})$")
# Títulos de sección numerados: "9. Cruz Roja Colombiana", "TAREA 1.2: BÚSQUEDA Y RESCATE",
# "ANEXO FUNCIONAL II: SALUD Y SANEAMIENTO". Las etiquetas sueltas en mayúsculas son
# columnas de tablas y no sirven de contexto.
_TITULO = re.compile(
    r"^(?:\d{1,2}\.\s+[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ ,.\-–/()]{3,70}|"
    r"TAREA\s+\d\.\d+:?\s+[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑ ,.\-–/()]{3,70}|"
    r"ANEXO FUNCIONAL\s+[IVX]+:?\s+[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑ ,.\-–/()]{3,70})$"
)

# Encabezados y pies que se repiten en cada página.
_RUIDO = re.compile(
    r"^(?:Estrategia Municipal para la Respuesta a Emergencias.*|"
    r"Alcald[ií]a de Manizales\s*[–-]\s*Unidad.*|\d{1,3}|Actualizaci[oó]n 2024|"
    r"Responsabilidades y funciones de las instituciones en la EMRE|ANEXO|TAREA|FUNCIONES|[•●]+)$",
    re.IGNORECASE,
)
_INDICE = re.compile(r"\.{6,}")  # "1. POLICÍA NACIONAL ........ 4"
_ORACION = re.compile(r"(?<=[.;:])\s+(?=[A-ZÁÉÍÓÚÑ●•\d])")


def titulo(pdf: Path) -> str:
    """'EMRE MANIZALES 2024 - Anexo 1.pdf' → 'EMRE Manizales 2024 - Anexo 1'."""
    return pdf.stem.replace("EMRE MANIZALES", "EMRE Manizales")


def paginas(pdf: Path) -> list[str]:
    """Texto de cada página (pdftotext separa las páginas con \\f)."""
    out = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", str(pdf), "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return out.split("\f")


def _lineas(pagina: str) -> list[str]:
    """Líneas limpias; "9." sola + "Cruz Roja Colombiana" en la siguiente → una línea."""
    crudas = [" ".join(x.split()) for x in pagina.splitlines()]
    crudas = [x for x in crudas if x]
    out: list[str] = []
    saltar = False
    for i, linea in enumerate(crudas):
        if saltar:
            saltar = False
            continue
        if re.fullmatch(r"\d{1,2}\.", linea) and i + 1 < len(crudas):
            linea, saltar = f"{linea} {crudas[i + 1]}", True
        out.append(linea)
    return out


def es_titulo(linea: str) -> bool:
    if not _TITULO.match(linea) or _INDICE.search(linea):
        return False
    # "2. Ofrecer apoyo logístico en …." es un ítem de lista, no el nombre de una entidad
    return not (
        linea[0].isdigit() and (linea.endswith(".") or len(linea.split()) > MAX_PALABRAS_TITULO)
    )


def _unir(lineas: list[str]) -> str:
    texto = " ".join(lineas)
    texto = re.sub(r"(\w)- (\w)", r"\1\2", texto)  # palabras partidas al final de línea
    return texto.replace(" ● ", " • ").replace("●", "•")


def segmentos(pagina: str, seccion: str) -> tuple[list[tuple[str, str]], str]:
    """Trozos de la página con el título vigente en cada uno, y el último título (para la
    página siguiente). Un título a mitad de página abre un trozo nuevo."""
    trozos: list[tuple[str, list[str]]] = [(seccion, [])]
    for linea in _lineas(pagina):
        if es_titulo(linea):
            seccion = linea.strip(" :")
            trozos.append((seccion, []))
        elif not _RUIDO.match(linea) and not _INDICE.search(linea):
            trozos[-1][1].append(linea)
    return [(t, _unir(ls)) for t, ls in trozos if ls], seccion


def limpiar(pagina: str) -> str:
    """Texto de la página sin encabezados, pies ni índice."""
    return _unir([x for x in _lineas(pagina) if not _RUIDO.match(x) and not _INDICE.search(x)])


def es_tabla(texto: str) -> bool:
    tokens = texto.split()
    if len(tokens) < MIN_PALABRAS:
        return True
    return sum(bool(_CELDA.match(t)) for t in tokens) / len(tokens) > MAX_CELDAS


def partir(texto: str) -> list[str]:
    """Pasajes de MIN_PALABRAS a MAX_PALABRAS, cortando entre oraciones."""
    pasajes: list[str] = []
    actual: list[str] = []
    for oracion in _ORACION.split(texto):
        palabras = oracion.split()
        if actual and len(actual) + len(palabras) > MAX_PALABRAS:
            pasajes.append(" ".join(actual))
            actual = []
        actual.extend(palabras)
        while len(actual) > MAX_PALABRAS:  # una "oración" enorme (listas sin punto)
            pasajes.append(" ".join(actual[:MAX_PALABRAS]))
            actual = actual[MAX_PALABRAS:]
    if actual:
        if pasajes and len(actual) < MIN_PALABRAS:
            pasajes[-1] += " " + " ".join(actual)
        else:
            pasajes.append(" ".join(actual))
    return [p for p in pasajes if len(p.split()) >= MIN_PALABRAS]


def pasajes_oficiales(fuentes: Path = FUENTES) -> list[dict]:
    corpus: list[dict] = []
    for pdf in sorted(fuentes.glob("*.pdf")):
        seccion = ""  # título vigente: da contexto a los pasajes ("19. EFIGAS SA ESP: …")
        for n, pagina in enumerate(paginas(pdf), 1):
            trozos, seccion = segmentos(pagina, seccion)
            if es_tabla(" ".join(t for _, t in trozos)):
                continue
            for contexto, texto in trozos:
                for trozo in partir(texto):
                    corpus.append(
                        {
                            "document": titulo(pdf),
                            "section": f"p. {n}",
                            "version": VERSION,
                            "text": f"{contexto}: {trozo}" if contexto else trozo,
                            "is_example": False,
                        }
                    )
    return corpus


def resumenes_equipo(ruta: Path = RESUMENES) -> list[dict]:
    if not ruta.is_file():
        return []
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    return [{**d, "is_example": True} for d in datos]


def main() -> int:
    if shutil.which("pdftotext") is None:
        print("Falta pdftotext (sudo apt install poppler-utils).", file=sys.stderr)
        return 1
    oficiales = pasajes_oficiales()
    equipo = resumenes_equipo()
    SALIDA.write_text(
        json.dumps(oficiales + equipo, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    docs = len({p["document"] for p in oficiales})
    print(
        f"{len(oficiales)} pasajes de {docs} PDF oficiales + {len(equipo)} resúmenes del "
        f"equipo (ejemplo) → {SALIDA.relative_to(DATA.parent)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
