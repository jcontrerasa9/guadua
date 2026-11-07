# SARD

Prototipo para la hackathon SARD: reportes de emergencia distribuidos que funcionan **sin Internet y sin API key**.
Diseño completo en [docs/diseno.md](docs/diseno.md); plan de trabajo en [docs/plan.md](docs/plan.md).

## Instalación (Python 3.13)

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # opcional: solo para usar un LLM
```

## Pruebas

```bash
ruff check . && ruff format --check .
pytest
node --test "tests/js/*.test.mjs"
```

## Ejecutar

```bash
docker compose up
```
