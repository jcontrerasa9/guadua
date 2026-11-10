"""Pruebas de integridad para data/barrios.json.

Verifica:
- El archivo existe y es un JSON válido.
- Cada entrada tiene los campos exactos: code, name, lat, lon.
- Los códigos son enteros únicos, positivos y dentro del rango de 40 bits (1 <= code <= 2**40 - 1).
- Los nombres son cadenas no vacías.
- Las coordenadas son float o None.
- Contiene los 6 barrios iniciales de docs/diseno.md.
"""

from __future__ import annotations

import json
from pathlib import Path

MAX_UINT40 = 2**40 - 1
_BARRIOS_JSON = Path(__file__).resolve().parent.parent / "data" / "barrios.json"

BARRIOS_DISENO = {
    "El Cable",
    "Av. Santander",
    "Chipre",
    "Centro",
    "Palermo",
    "Milán",
}


def test_barrios_json_estructura_y_campos() -> None:
    assert _BARRIOS_JSON.is_file(), f"No existe el archivo {_BARRIOS_JSON}"

    with _BARRIOS_JSON.open(encoding="utf-8") as f:
        data = json.load(f)

    assert isinstance(data, list), "barrios.json debe contener una lista"
    assert len(data) >= len(BARRIOS_DISENO), f"Se esperaban al menos {len(BARRIOS_DISENO)} barrios"

    codigos_vistos: set[int] = set()
    nombres_vistos: set[str] = set()

    for item in data:
        assert isinstance(item, dict), f"Entrada inválida en barrios.json: {item!r}"
        assert set(item.keys()) == {
            "code",
            "name",
            "lat",
            "lon",
        }, f"Campos no coinciden con la especificación: {set(item.keys())}"

        code = item["code"]
        name = item["name"]
        lat = item["lat"]
        lon = item["lon"]

        # Código: entero único, positivo y dentro de 40 bits
        assert isinstance(code, int) and not isinstance(code, bool), f"code debe ser int: {code!r}"
        assert 1 <= code <= MAX_UINT40, f"code fuera de rango [1, 2**40 - 1]: {code}"
        assert code not in codigos_vistos, f"code duplicado: {code}"
        codigos_vistos.add(code)

        # Nombre: cadena no vacía
        assert isinstance(name, str) and name.strip(), f"name debe ser cadena no vacía: {name!r}"
        nombres_vistos.add(name.strip())

        # Coordenadas: float o None
        assert lat is None or isinstance(lat, float), f"lat debe ser float o None: {lat!r}"
        assert lon is None or isinstance(lon, float), f"lon debe ser float o None: {lon!r}"

    # Verificar que los barrios citados en diseno.md están incluidos
    faltantes = BARRIOS_DISENO - nombres_vistos
    assert not faltantes, f"Faltan barrios requeridos de diseno.md: {faltantes}"
