# AGENTS.md

Reglas que deben seguir los asistentes de IA que usamos en este repo (Claude Code, Antigravity u otros). Los responsables del código son las personas del equipo; los asistentes son herramientas. Diseño: `docs/diseno.md`. Tareas: `docs/plan.md`.

## Comandos

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .   # lint y formato
pytest                                  # pruebas Python
node --test "tests/js/*.test.mjs"       # pruebas del codificador JS
```

## Estructura

Guía completa de carpetas en `README.md` (sección "Guía de carpetas"). Resumen: `citizen/` PWA, `src/guadua/` backend, `src/guadua/dashboard/` tablero, `tests/` pruebas (`js/` y `fixtures/`), `data/` datos, `docs/` diseño y plan.

## Reglas que no se pueden romper

1. **El piso de urgencia nunca baja.** Ningún código, agente o LLM puede fijar una urgencia menor que la que fijaron las reglas o el clasificador.
2. **Los eventos son inmutables.** Solo se agregan; nunca se editan ni se borran.
3. **Todo funciona sin Internet ni API key.** Las pruebas usan LLM simulado; sin LLM, el caso va a revisión humana.
4. **Cada componente va detrás de una interfaz** (`Retriever`, proveedor de LLM, transporte, almacenamiento). Nada depende de una implementación concreta.
5. **Toda funcionalidad lleva pruebas** y deja `ruff` y `pytest` en verde.
6. **No se cambian contratos sin pedirlo explícitamente:** `src/guadua/protocol/header.py`, `citizen/protocol/header.js`, `src/guadua/store/events.py`, `src/guadua/classifier/urgency.py`, las interfaces en `providers/`, `knowledge/` y `store/`, y todo `tests/fixtures/`.
7. **Solo se trabaja en las tareas asignadas** en `docs/plan.md`; al terminar, se marca la tarea ahí.
8. **Nunca se lee ni se sube `.env`.** Usa `.env.example` como referencia.
