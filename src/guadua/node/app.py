"""App FastAPI del nodo: las rutas de nivel 1 de docs/contratos.md y las pantallas.

    /api/...        la API (formatos en node/api.py)
    /tablero/       tablero del comandante (src/guadua/dashboard)
    /ciudadano/     PWA ciudadana (citizen/)
    /data/...       barrios.json y el diccionario, que usan las pantallas

Los errores 4xx devuelven ApiError ({"error": "..."}). CORS abierto: el tablero servido por
un nodo consulta también a los demás cuando cambia de nodo.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from guadua.node.api import (
    DecisionAck,
    DecisionRequest,
    Health,
    IncidentDetail,
    IncidentList,
    MessageAck,
    PeopleList,
    QuestionList,
)
from guadua.node.intake import IntakeError
from guadua.node.service import Conflict, Node, NotFound
from guadua.node.views import incident_cards, incident_detail

logger = logging.getLogger("guadua.node")

ROOT = Path(__file__).resolve().parents[3]
DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
CITIZEN_DIR = ROOT / "citizen"
DATA_FILES = {"barrios.json", "diccionario_manizales.txt"}
VIEW_PREFIXES = ("/tablero", "/ciudadano", "/data/")


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def create_app(node: Node, *, gossip_interval: float | None = 2.0) -> FastAPI:
    """`gossip_interval=None` no arranca el ciclo de gossip ni el de resiliencia (útil en
    pruebas). La resiliencia (N12) hace una ronda con la misma frecuencia que el gossip."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        tasks: list[asyncio.Task] = []
        transport = node.gossip.transport if node.gossip else None
        if transport is not None and hasattr(transport, "start"):
            await transport.start()
        if node.gossip is not None and gossip_interval is not None:
            tasks.append(asyncio.create_task(node.gossip.run(gossip_interval, stop)))
        if gossip_interval is not None:
            tasks.append(asyncio.create_task(node.resilience.run(gossip_interval, stop)))
        try:
            yield
        finally:
            stop.set()
            for task in tasks:
                await asyncio.wait_for(task, timeout=5)
            if transport is not None and hasattr(transport, "close"):
                transport.close()

    app = FastAPI(title=f"SARD · nodo {node.node_id}", lifespan=lifespan)
    app.state.node = node

    @app.middleware("http")
    async def _no_stale_views(request: Request, call_next):
        """Las pantallas se revalidan siempre: el navegador pregunta si cambiaron antes de
        reusar su copia (304 si no). Así una versión nueva nunca queda escondida."""
        response = await call_next(request)
        if request.url.path.startswith(VIEW_PREFIXES):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"]
    )

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        return _error(f"petición inválida: {where} {first.get('msg', '')}".strip(), 400)

    # --- API -------------------------------------------------------------------------------

    @app.get("/api/health", response_model=Health)
    async def health() -> Health:
        return node.health()

    @app.post("/api/messages", response_model=MessageAck)
    async def messages(request: Request) -> MessageAck | JSONResponse:
        try:
            return node.receive_message(await request.body())
        except IntakeError as exc:
            return _error(str(exc), 400)

    @app.get("/api/incidents", response_model=IncidentList)
    async def incidents() -> IncidentList:
        return IncidentList(
            node=node.node_id, incidents=tuple(incident_cards(node.store.since({})))
        )

    @app.get("/api/incidents/{incident_id}", response_model=IncidentDetail)
    async def incident(incident_id: str) -> IncidentDetail | JSONResponse:
        detail = incident_detail(node.store.since({}), incident_id)
        if detail is None:
            return _error(f"no existe el incidente {incident_id}", 404)
        return detail

    @app.post("/api/decisions", response_model=DecisionAck, status_code=201)
    async def decisions(request: DecisionRequest) -> DecisionAck | JSONResponse:
        try:
            return node.decide(request)
        except NotFound as exc:
            return _error(str(exc), 404)
        except Conflict as exc:
            return _error(str(exc), 409)

    # Nivel 2: existen con su formato, todavía vacías.
    @app.get("/api/questions/{device_id}", response_model=QuestionList)
    async def questions(device_id: str) -> QuestionList:
        return QuestionList(questions=())

    @app.get("/api/people", response_model=PeopleList)
    async def people() -> PeopleList:
        return PeopleList(people=())

    # --- Pantallas -------------------------------------------------------------------------

    @app.get("/data/{name}", response_model=None)
    async def data(name: str) -> FileResponse | JSONResponse:
        if name not in DATA_FILES or not (ROOT / "data" / name).is_file():
            return _error(f"no existe /data/{name}", 404)
        return FileResponse(ROOT / "data" / name)

    @app.get("/", response_model=None)
    async def root() -> RedirectResponse:
        return RedirectResponse("/tablero/")

    if DASHBOARD_DIR.is_dir():
        app.mount("/tablero", StaticFiles(directory=DASHBOARD_DIR, html=True), name="tablero")
    if CITIZEN_DIR.is_dir():
        app.mount("/ciudadano", StaticFiles(directory=CITIZEN_DIR, html=True), name="ciudadano")
    return app
