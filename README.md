# SARD: Guadua

Prototipo para la hackathon SARD: reportes de emergencia distribuidos que funcionan **sin Internet**.
Diseño completo en [docs/diseno.md](docs/diseno.md); plan de trabajo en [docs/plan.md](docs/plan.md).

## Entregables

| Entregable | Dónde |
| :-- | :-- |
| Prototipo funcional | `docker compose up` (ver [Ejecutar](#ejecutar)) |
| Código fuente | Este repositorio |
| Documento de arquitectura | [docs/arquitectura.md](docs/arquitectura.md) |
| Fichas de agentes | [docs/fichas/](docs/fichas/) |
| Protocolo de comunicación | [docs/protocolo.md](docs/protocolo.md) |
| Resultados de pruebas | [docs/resultados.md](docs/resultados.md) |

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
python -m guadua.classifier.evaluate    # sensibilidad en críticos, matriz y tiempos del triaje
python -m guadua.agents.triaje_eval     # cuánto llega al agente de triaje; con LLM, cómo resuelve
python -m guadua.bench                  # banco de fallos: tabla de métricas de §15 (semilla fija, ~15 s)
python -m guadua.knowledge.eval_rag     # RAG: páginas oficiales de la EMRE y resúmenes del equipo
python -m guadua.knowledge.ingest       # rehace data/emre_passages.json desde los PDF (necesita pdftotext)
```

## Ejecutar

Requisitos: Docker con Compose. La primera construcción descarga las dependencias y el modelo de Whisper (`small`); después todo corre sin Internet.

```bash
docker compose up -d --build
```

| Qué | Dirección |
| :-- | :-- |
| PWA ciudadana | <http://localhost:8000/ciudadano/> |
| Tablero del comandante | <http://localhost:8000/tablero/> |
| Nodos B y C | <http://localhost:8001>, <http://localhost:8002> |
| Proxy del canal (fallos inyectados) | <http://localhost:8080> |

El tablero cambia solo de nodo si el suyo cae. La barra "Modo demostración" del tablero cambia el perfil del canal (Wi-Fi, LoRa, SMS), lanza una ráfaga, inyecta un clip de radio, apaga el nodo A y muestra los hashes de convergencia.

**LLM (opcional).** Sin LLM, los casos dudosos quedan "ambiguo, revisar", las preguntas salen de reglas y las fichas de plantilla. Para activarlo, copia `.env.example` a `.env` y completa una opción (Compose lee el `.env`):

```
LLM_PROVIDER=auto                               # gemini, ollama, auto o none
GEMINI_API_KEY=...                               # con Internet
GEMINI_MODEL=...
OLLAMA_HOST=http://host.docker.internal:11434    # Ollama en el equipo, visto desde Docker
OLLAMA_MODEL=qwen2.5:7b
```

Con Ollama en el equipo: `OLLAMA_HOST=0.0.0.0 ollama serve` y `ollama pull qwen2.5:7b`. El orden es Gemini (nube) → Ollama (local) → ninguno.

**GPS en el celular.** El navegador solo entrega el GPS con HTTPS; sin él, la PWA usa el barrio.

## Guía de carpetas

¿Dónde va cada cosa? Quién hace cada tarea está en [docs/plan.md](docs/plan.md). Los archivos marcados como **contrato** no se cambian sin acordarlo entre los dos; la lista y los formatos están en [docs/contratos.md](docs/contratos.md).

```
guadua/
├── citizen/              PWA del ciudadano (HTML + JS, sin frameworks)
│   └── protocol/         header.js: cabecera de 21 B en JS (contrato)
├── src/guadua/           Backend en Python: lo que corre en cada nodo
│   ├── protocol/         Formato binario: cabecera (header.py, contrato), texto con diccionario (text.py, contrato), fragmentos y ACK (fragments.py, contrato)
│   ├── channel/          Canal simulado (Wi-Fi, radio/LoRa, SMS) y proxy que inyecta fallos
│   ├── store/            Eventos (events.py y event_types.py, contrato), SQLite y gossip entre nodos
│   ├── classifier/       Urgencia (urgency.py, contrato: el piso nunca baja), reglas y clasificador
│   ├── agents/           Agentes con LLM: recepción, triaje, correlación, coordinación, vigía
│   ├── services/         Servicios sin LLM: cola de comunicación, resiliencia
│   ├── radio/            Radio VHF (D7): faster-whisper, clips guardados, lectura del texto y `python -m guadua.radio`
│   ├── bench/            Banco de fallos (N15): pérdida, partición y caída sobre el canal simulado
│   ├── knowledge/        Matriz EMRE, recursos y RAG (retriever.py, contrato)
│   ├── providers/        Interfaces de LLM, transporte y transcriptor (contrato) y sus implementaciones
│   ├── node/             Nodo FastAPI; api.py: rutas y formatos que usan la PWA y el tablero (contrato)
│   └── dashboard/        Tablero del comandante (HTML + JS servido por los nodos)
├── tests/                Pruebas de Python (pytest)
│   ├── js/               Pruebas de JS (node --test)
│   ├── fixtures/         Ejemplos compartidos por Python y JS (contrato)
│   └── fakes.py          Versiones falsas de las interfaces para probar sin red
├── data/                 Datos sintéticos, diccionario, barrios, mapas, clips de radio, documentos
├── docs/                 diseno.md (qué y por qué), plan.md (quién y qué), contratos.md, fichas/
├── .github/              CI y plantilla de pull request
├── AGENTS.md             Reglas para los asistentes de IA (CLAUDE.md lo importa)
├── pyproject.toml        Dependencias y configuración de ruff y pytest
├── Dockerfile            Imagen de un nodo
├── docker-compose.yml    Levanta los nodos A, B y C
└── .env.example          Variables de ejemplo; se copia a .env, que nunca se sube
```

**Para ubicarte rápido:**

- ¿Algo que ve el ciudadano en el celular? → `citizen/`
- ¿Algo que ve el comandante? → `src/guadua/dashboard/`
- ¿Cómo viajan los mensajes? → `protocol/` (qué bytes) y `channel/` (por dónde)
- ¿Cómo se guardan y se sincronizan los datos entre nodos? → `store/`
- ¿Qué tan urgente es un reporte? → `classifier/` y luego `agents/` (solo en casos dudosos)
- ¿A quién llamar y con qué recursos? → `knowledge/` y el agente de coordinación en `agents/`
