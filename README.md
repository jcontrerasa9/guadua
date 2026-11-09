# SARD: Guadua

Prototipo para la hackathon SARD: reportes de emergencia distribuidos que funcionan **sin Internet**.
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
│   ├── services/         Servicios sin LLM: cola de comunicación, resiliencia, transcripción de radio
│   ├── knowledge/        Matriz EMRE, recursos y RAG (retriever.py, contrato)
│   ├── providers/        Interfaces de LLM y transporte (contrato) y sus implementaciones
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
