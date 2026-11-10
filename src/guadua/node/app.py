"""App FastAPI del nodo: las rutas de docs/contratos.md y las pantallas.

    /api/...        la API (formatos en node/api.py); /api/radio... es la radio VHF (D7)
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
import re
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
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
    RadioAck,
    RadioList,
    TranscriptionRequest,
)
from guadua.node.intake import IntakeError
from guadua.node.service import Conflict, Node, NotFound
from guadua.node.views import incident_cards, incident_detail, pending_questions, radio_clips
from guadua.radio.clips import EXTENSIONS, ClipError
from guadua.store.event_types import AUDIO_ID_PATTERN

logger = logging.getLogger("guadua.node")

ROOT = Path(__file__).resolve().parents[3]
DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
CITIZEN_DIR = ROOT / "citizen"
DATA_FILES = {"barrios.json", "diccionario_manizales.txt"}
VIEW_PREFIXES = ("/tablero", "/ciudadano", "/data/")
RADIO_DIR = ROOT / "data" / "radio"
DEMO_ID = re.compile(r"^[a-z0-9_]{1,64}$")
AUDIO_ID = re.compile(AUDIO_ID_PATTERN)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def demo_clip(clip_id: str) -> tuple[Path, str] | None:
    """Un clip grabado por el equipo en data/radio/ (para la barra de simulación, D9)."""
    if not DEMO_ID.match(clip_id):
        return None
    for extension, content_type in EXTENSIONS.items():
        path = RADIO_DIR / f"{clip_id}{extension}"
        if path.is_file():
            return path, content_type
    return None


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

    @app.get("/api/questions/{device_id}", response_model=QuestionList)
    async def questions(device_id: str) -> QuestionList:
        pending = pending_questions(node.store.since({}), device_id, now=node.clock())
        return QuestionList(questions=tuple(pending))

    # Nivel 2: existe con su formato, todavía vacía.

    @app.get("/api/people", response_model=PeopleList)
    async def people() -> PeopleList:
        return PeopleList(people=())

    # Radio VHF (D7): el audio entra, se transcribe en otro hilo y se separa en reportes.

    @app.post("/api/radio", response_model=RadioAck)
    async def radio(request: Request) -> RadioAck | JSONResponse:
        try:
            return await node.receive_audio(
                await request.body(), request.headers.get("content-type")
            )
        except ClipError as exc:
            return _error(str(exc), 400)

    @app.post("/api/radio/demo/{clip_id}", response_model=RadioAck)
    async def radio_demo(clip_id: str) -> RadioAck | JSONResponse:
        found = demo_clip(clip_id)
        if found is None:
            return _error(f"no existe el clip de demostración {clip_id} en data/radio/", 404)
        path, content_type = found
        try:
            return await node.receive_audio(path.read_bytes(), content_type, name=clip_id)
        except ClipError as exc:
            return _error(str(exc), 400)

    @app.get("/api/radio", response_model=RadioList)
    async def radio_list() -> RadioList:
        clips = radio_clips(node.store.since({}), node.clips.clips(), node.radio_notes)
        return RadioList(node=node.node_id, clips=tuple(clips))

    @app.get("/api/radio/{audio_id}/audio", response_model=None)
    async def radio_audio(audio_id: str) -> Response:
        found = node.clips.get(audio_id) if AUDIO_ID.match(audio_id) else None
        if found is None:
            return _error(f"este nodo no tiene el audio {audio_id}", 404)
        clip, data = found
        return Response(content=data, media_type=clip.content_type)

    @app.post("/api/radio/{audio_id}/transcription", response_model=RadioAck, status_code=201)
    async def radio_transcription(
        audio_id: str, request: TranscriptionRequest
    ) -> RadioAck | JSONResponse:
        if not AUDIO_ID.match(audio_id):
            return _error(f"id de clip inválido: {audio_id}", 404)
        try:
            return node.correct_transcription(audio_id, request)
        except NotFound as exc:
            return _error(str(exc), 404)

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
