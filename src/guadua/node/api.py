"""API HTTP del nodo: formatos de petición y respuesta. CONTRATO: ver docs/contratos.md.

Lo usan la PWA (citizen/) y el tablero (dashboard/). La implementación de las rutas es la
tarea N6; aquí solo está lo que ambos lados deben respetar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guadua.classifier.urgency import Urgency
from guadua.protocol.header import Incident
from guadua.store.event_types import (
    AUDIO_ID_PATTERN,
    EVENT_ID_PATTERN,
    REPORT_ID_PATTERN,
    Citation,
    Location,
    QuestionKind,
)

API_PREFIX = "/api"


@dataclass(frozen=True, slots=True)
class Route:
    method: Literal["GET", "POST"]
    path: str
    request: str  # modelo, "bytes" o "—"
    response: str
    used_by: str
    level: int  # nivel del plan en que debe existir


ROUTES: tuple[Route, ...] = (
    Route("GET", "/api/health", "—", "Health", "PWA, tablero", 1),
    Route("POST", "/api/messages", "bytes", "MessageAck | ApiError", "PWA", 1),
    Route("GET", "/api/incidents", "—", "IncidentList", "tablero", 1),
    Route("GET", "/api/incidents/{incident_id}", "—", "IncidentDetail | ApiError", "tablero", 1),
    Route("POST", "/api/decisions", "DecisionRequest", "DecisionAck | ApiError", "tablero", 1),
    Route("GET", "/api/questions/{device_id}", "—", "QuestionList", "PWA", 2),
    Route("GET", "/api/people", "—", "PeopleList", "tablero", 2),
    Route("POST", "/api/radio", "bytes (audio/*)", "RadioAck | ApiError", "script, tablero", 2),
    Route(
        "POST", "/api/radio/demo/{clip_id}", "—", "RadioAck | ApiError", "barra de simulación", 2
    ),
    Route("GET", "/api/radio", "—", "RadioList", "tablero", 2),
    Route("GET", "/api/radio/{audio_id}/audio", "—", "bytes (audio/*) | ApiError", "tablero", 2),
    Route(
        "POST",
        "/api/radio/{audio_id}/transcription",
        "TranscriptionRequest",
        "RadioAck | ApiError",
        "tablero",
        2,
    ),
)


class Schema(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ApiError(Schema):
    """Cuerpo de toda respuesta 4xx."""

    error: str


class Health(Schema):
    """GET /api/health. El tablero lo usa para elegir el primer nodo vivo."""

    node: str
    version: str
    peers_alive: tuple[str, ...] = ()


class MessageAck(Schema):
    """POST /api/messages → 200. Cuerpo de la petición: bytes (cabecera de 21 B + texto).

    El 200 es el ACK: la PWA marca el reporte como "Recibido". Un repetido también recibe 200,
    con duplicate=True, y no se guarda dos veces. Bytes inválidos → 400 con ApiError.
    """

    report_id: str = Field(pattern=REPORT_ID_PATTERN)
    status: Literal["recibido"] = "recibido"
    duplicate: bool = False


class IncidentCard(Schema):
    """Una tarjeta del tablero: lo vital a primera vista (diseño §3.2)."""

    incident_id: str = Field(min_length=1)
    level: Urgency
    incident: Incident
    title: str  # p. ej. "Colapso con 3 atrapados"
    location: Location
    confidence: float | None = Field(default=None, ge=0, le=1)  # None: sin correlación aún
    rumor: bool = False
    verify_now: bool = False
    risk_warning: str | None = None
    suggestion: str | None = None  # entidad y recursos sugeridos
    message: str | None = None  # "qué decir"
    status: Literal["sin_ficha", "pendiente", "aprobada", "editada", "rechazada", "conflicto"]
    ficha_id: str | None = Field(default=None, pattern=EVENT_ID_PATTERN)
    report_count: int = Field(ge=1)
    received_at: float  # primer reporte, hora del nodo (Unix, s): para "hace 4 min"
    updated_at: float


class IncidentList(Schema):
    """GET /api/incidents. Ordenada por `level` (mayor primero) y luego por antigüedad."""

    node: str
    incidents: tuple[IncidentCard, ...]


class AgentStep(Schema):
    agent: str
    detail: str


class IncidentDetail(IncidentCard):
    """GET /api/incidents/{incident_id}: la tarjeta más el desplegable "por qué está aquí"."""

    report_ids: tuple[str, ...]
    rules: tuple[str, ...] = ()
    probabilities: dict[str, float] = Field(default_factory=dict)
    sources: tuple[str, ...] = ()  # p. ej. "ciudadano directo", "radio de segunda mano"
    contradictions: tuple[str, ...] = ()
    citations: tuple[Citation, ...] = ()
    steps: tuple[AgentStep, ...] = ()


class DecisionRequest(Schema):
    """POST /api/decisions. 404 si la ficha no existe; 409 si ya tiene una decisión."""

    ficha_id: str = Field(pattern=EVENT_ID_PATTERN)
    action: Literal["aprobar", "editar", "rechazar"]
    edited_message: str | None = None
    operator: str = Field(min_length=1)

    @model_validator(mode="after")
    def _edit_needs_message(self) -> DecisionRequest:
        if (self.action == "editar") != (self.edited_message is not None):
            raise ValueError("edited_message va solo, y siempre, con la acción editar")
        return self


class DecisionAck(Schema):
    """POST /api/decisions → 201."""

    event_id: str = Field(pattern=EVENT_ID_PATTERN)


class Question(Schema):
    """Nivel 2. Pregunta de seguimiento; se responde con un mensaje binario RESPUESTA (tipo 7,
    formato en docs/contratos.md). "No sé" siempre se ofrece, aunque no esté en `options`."""

    question_id: str = Field(pattern=EVENT_ID_PATTERN)  # id del evento pregunta_seguimiento
    report_id: str = Field(pattern=REPORT_ID_PATTERN)
    text: str  # p. ej. "¿Huele a gas?"
    kind: QuestionKind = "si_no"
    options: tuple[str, ...] = ()  # solo con kind "opciones"


class QuestionList(Schema):
    """GET /api/questions/{device_id}: las preguntas sin responder de ese dispositivo, como
    máximo una por reporte y de los últimos 5 minutos, la del reporte más reciente primero."""

    questions: tuple[Question, ...]


class Person(Schema):
    """Nivel 2. Registro de personas a salvo."""

    report_id: str = Field(pattern=REPORT_ID_PATTERN)
    name: str | None = None
    barrio_label: str | None = None
    received_at: float


class PeopleList(Schema):
    people: tuple[Person, ...]


class RadioAck(Schema):
    """Nivel 2 (D7). POST /api/radio → 200 (cuerpo: el audio, con su Content-Type), POST
    /api/radio/demo/{clip_id} → 200 y POST /api/radio/{audio_id}/transcription → 201.

    `transcrito`: ya hay `transcripcion_radio` (`transcription_id`) y el agente de recepción
    la separa en reportes. `pendiente`: sin transcriptor o el audio no se entendió; el
    operador escribe el texto (`note` dice por qué). El mismo audio otra vez → duplicate.
    """

    audio_id: str = Field(pattern=AUDIO_ID_PATTERN)
    status: Literal["transcrito", "pendiente"]
    transcription_id: str | None = Field(default=None, pattern=EVENT_ID_PATTERN)
    text: str | None = None
    note: str | None = None
    duplicate: bool = False


class RadioClip(Schema):
    """Un clip de radio en el tablero, con su transcripción vigente (la más reciente de la
    cadena de correcciones) y los reportes que salieron de ella."""

    audio_id: str = Field(pattern=AUDIO_ID_PATTERN)
    status: Literal["transcrito", "pendiente"]
    received_at: float
    has_audio: bool  # este nodo tiene el audio y lo sirve en /api/radio/{audio_id}/audio
    name: str | None = None
    transcription_id: str | None = Field(default=None, pattern=EVENT_ID_PATTERN)
    text: str | None = None
    source: Literal["whisper", "operador"] | None = None
    model: str | None = None
    operator: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    duration_s: float | None = Field(default=None, ge=0)
    corrections: int = Field(default=0, ge=0)
    report_ids: tuple[str, ...] = ()
    split_by: str | None = None  # "reglas" o el modelo que separó
    note: str | None = None  # por qué está pendiente


class RadioList(Schema):
    """GET /api/radio: los clips, el más reciente primero."""

    node: str
    clips: tuple[RadioClip, ...]


class TranscriptionRequest(Schema):
    """POST /api/radio/{audio_id}/transcription: el operador escribe el texto de un clip
    pendiente o corrige el vigente. 404 si el nodo no conoce el clip. Si es directo o de
    segunda mano se decide por lo que dice el texto ("estamos en el sitio")."""

    text: str = Field(min_length=1, max_length=2000)
    operator: str = Field(min_length=1)


SCHEMAS: dict[str, type[Schema]] = {
    cls.__name__: cls
    for cls in (
        ApiError,
        Health,
        MessageAck,
        IncidentCard,
        IncidentList,
        IncidentDetail,
        DecisionRequest,
        DecisionAck,
        QuestionList,
        PeopleList,
        RadioAck,
        RadioList,
        TranscriptionRequest,
    )
}
