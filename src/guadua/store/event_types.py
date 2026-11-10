"""Catálogo de tipos de evento y el formato de su payload. CONTRATO: ver docs/contratos.md.

Cada nodo valida un evento con `validate_event` antes de guardarlo. Quien produce un tipo
de evento y quien lo consume (otro agente, el tablero) dependen de este catálogo.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from guadua.classifier.urgency import Urgency
from guadua.protocol.header import Incident, MessageType, People
from guadua.store.events import Event, EventCategory

type ReportId = str
"""Identificador de un reporte: dispositivo en 8 dígitos hex + "-" + consecutivo (a1b2c3d4-7)."""

REPORT_ID_PATTERN = r"^[0-9a-f]{8}-[0-9]+$"
NODE_ID_PATTERN = r"^[A-Za-z0-9]+$"
EVENT_ID_PATTERN = r"^[A-Za-z0-9]+:[1-9][0-9]*$"
AUDIO_ID_PATTERN = r"^[0-9a-f]{16}$"  # primeros 16 hex del SHA-256 del audio

type Channel = Literal["wifi", "radio", "sms", "vhf"]


def report_id(device_id: int, seq: int) -> ReportId:
    return f"{device_id:08x}-{seq}"


def event_id_str(event: Event) -> str:
    """Forma de texto de un EventId para JSON: "A:12"."""
    return f"{event.origin_node}:{event.seq}"


class Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Location(Payload):
    """Ubicación con incertidumbre: se dibuja como círculo de radio `radius_m`."""

    geohash: str | None = Field(default=None, min_length=7, max_length=7)
    barrio: int | None = Field(default=None, ge=0)
    radius_m: int = Field(ge=0)
    label: str | None = None  # nombre legible, p. ej. "El Cable"

    @model_validator(mode="after")
    def _exactly_one(self) -> Location:
        if (self.geohash is None) == (self.barrio is None):
            raise ValueError("se requiere exactamente uno: geohash o barrio")
        return self


class Citation(Payload):
    document: str
    section: str
    version: str
    is_example: bool = False


# --- Hechos: los crea el nodo que los recibe -------------------------------------------


class Reporte(Payload):
    """Reporte o SOS recibido. Copia fiel de la cabecera más el texto ya descomprimido.

    **Por radio (D7, `channel="vhf"`)** no hay cabecera: el agente de recepción separa una
    `transcripcion_radio` en uno o más reportes, cada uno con lo que dijo la radio y nada más.
    `audio_id` liga los reportes al mismo clip y el evento lleva la transcripción en
    `refs[0]`. `report_id` = CRC32 del id de la transcripción ("A:12") + número de parte;
    `raw_hex` va vacío; `flags` solo marca lo que se dijo (observación directa, atrapados…);
    sin lugar dicho, `geohash` y `barrio` van en None. `split_by` dice quién lo separó:
    "reglas" o el modelo ("gemini/…").
    """

    report_id: str = Field(pattern=REPORT_ID_PATTERN)
    msg_type: Literal[MessageType.REPORTE, MessageType.SOS_ESCOMBROS]
    incident: Incident
    people: People
    flags: int = Field(ge=0, le=255)
    geohash: str | None = None
    barrio: int | None = None
    device_time: int = Field(ge=0)
    text: str | None = None
    channel: Channel
    raw_hex: str  # bytes originales, para auditoría
    audio_id: str | None = Field(default=None, pattern=AUDIO_ID_PATTERN)  # solo por radio
    split_by: str | None = Field(default=None, min_length=1)  # solo por radio

    @model_validator(mode="after")
    def _radio_fields(self) -> Reporte:
        radio = self.channel == "vhf"
        if radio != (self.audio_id is not None) or radio != (self.split_by is not None):
            raise ValueError("audio_id y split_by van solo, y siempre, con el canal vhf")
        if radio and (self.raw_hex or self.msg_type != MessageType.REPORTE or not self.text):
            raise ValueError("un reporte de radio es un REPORTE con texto y sin bytes originales")
        return self


class ASalvo(Payload):
    """Nivel 2. "Estoy a salvo"."""

    report_id: str = Field(pattern=REPORT_ID_PATTERN)
    name: str | None = None
    barrio: int | None = Field(default=None, ge=0)


class TranscripcionRadio(Payload):
    """Nivel 2. Transcripción de un clip VHF (D7).

    La publica el nodo que recibió el audio, con `faster-whisper` (`source="whisper"`), o el
    operador desde el tablero (`source="operador"`) cuando el audio es ininteligible o hay
    que corregirlo. Una corrección es un evento nuevo con la transcripción anterior en
    `refs[0]` (sin refs si el operador escribe la primera): vale la que ninguna otra
    reemplaza. El audio no viaja en el evento: queda en el nodo que lo recibió.
    """

    audio_id: str = Field(pattern=AUDIO_ID_PATTERN)
    text: str = Field(min_length=1)
    direct: (
        bool  # algo del clip se vio en persona ("estamos en el sitio"); cada reporte dice lo suyo
    )
    source: Literal["whisper", "operador"] = "whisper"
    model: str | None = None  # p. ej. "faster-whisper/small-int8"
    duration_s: float | None = Field(default=None, ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)  # probabilidad media de Whisper
    operator: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _who(self) -> TranscripcionRadio:
        if (self.source == "operador") != (self.operator is not None):
            raise ValueError("operator va solo, y siempre, con source operador")
        return self


type QuestionKind = Literal["si_no", "opciones", "texto"]
type QuestionKey = Literal[
    "no_respira_o_sangrado", "gas_o_cables", "atrapados", "personas", "libre"
]
type AnswerKind = Literal["si", "no", "no_se", "opcion", "texto"]

MAX_OPTIONS = 4
MAX_QUESTION_ID = 32  # bytes UTF-8 del question_id en el mensaje de respuesta


class RespuestaSeguimiento(Payload):
    """Nivel 2. Respuesta del ciudadano a una pregunta del agente de recepción.

    `question_id` es el id del evento `pregunta_seguimiento` ("B:31"); el evento lleva esa
    pregunta en `refs[0]`. "No sé" siempre es una respuesta válida.
    """

    question_id: str = Field(pattern=EVENT_ID_PATTERN, max_length=MAX_QUESTION_ID)
    answer: AnswerKind
    option: int | None = Field(default=None, ge=0, lt=MAX_OPTIONS)  # solo con "opcion"
    text: str | None = Field(default=None, min_length=1)  # solo con "texto"

    @model_validator(mode="after")
    def _matches_answer(self) -> RespuestaSeguimiento:
        if (self.answer == "opcion") != (self.option is not None):
            raise ValueError("option va solo, y siempre, con la respuesta opcion")
        if (self.answer == "texto") != (self.text is not None):
            raise ValueError("text va solo, y siempre, con la respuesta texto")
        return self


class ConfirmacionCampo(Payload):
    """Nivel 2. Un rescatista confirma o descarta un incidente en el sitio."""

    incident_id: str
    confirmed: bool
    note: str | None = None


class Reasignacion(Payload):
    """Agente 5 (resiliencia). Quien lo publica dejó de oír a `from_node` y toma su trabajo
    pendiente hasta `up_to_seq`. El evento lleva `refs[0] = (from_node, up_to_seq)`."""

    from_node: str = Field(pattern=NODE_ID_PATTERN)
    up_to_seq: int = Field(ge=1)
    silent_s: float = Field(ge=0)  # segundos sin oír a `from_node` al decidir


# --- Derivados: solo los crea el nodo dueño del reporte --------------------------------


class ReporteNormalizado(Payload):
    """Agente 1 (recepción). Mientras no exista, el nodo lo genera con reglas simples."""

    report_id: str = Field(pattern=REPORT_ID_PATTERN)
    incident: Incident
    people: People
    location: Location
    direct_observation: bool
    vital_signs: tuple[str, ...] = ()  # p. ej. "atrapados", "no_respira"
    risk_signals: tuple[str, ...] = ()  # p. ej. "gas", "cables_caidos"
    text: str | None = None
    unknown_fields: tuple[str, ...] = ()  # lo que no llegó: no se inventa


class UrgenciaEstimada(Payload):
    """Agente 2 (triaje). `level` nunca es menor que `floor`."""

    report_id: str = Field(pattern=REPORT_ID_PATTERN)
    level: Urgency
    floor: Urgency
    source: Literal["reglas", "clasificador", "agente"]
    probabilities: dict[str, float] = Field(default_factory=dict)
    rules: tuple[str, ...] = ()
    risk_signals: tuple[str, ...] = ()
    verify_now: bool = False
    ambiguous: bool = False  # True → "ambiguo, revisar" (p. ej., sin LLM)
    steps: tuple[str, ...] = ()  # pasos del agente, para "por qué está aquí"

    @model_validator(mode="after")
    def _floor_never_drops(self) -> UrgenciaEstimada:
        if self.level < self.floor:
            raise ValueError("el piso de urgencia nunca baja")
        return self


class IncidenteAgrupado(Payload):
    """Agente 3 (correlación). Estado actual de un incidente; cada cambio es un evento nuevo."""

    incident_id: str = Field(min_length=1)
    report_ids: tuple[str, ...] = Field(min_length=1)
    link: Literal["seguro", "dudoso", "nuevo"]
    confidence: float = Field(ge=0, le=1)
    rumor: bool = False
    contradictions: tuple[str, ...] = ()


class Ficha(Payload):
    """Agente 6 (coordinación). Queda pendiente hasta una decisión humana."""

    incident_id: str = Field(min_length=1)
    responsible: str | None  # None → "sin responsable identificado, escalar al coordinador"
    units_available: int | None = Field(default=None, ge=0)
    risk_warning: str | None = None
    message: str
    citations: tuple[Citation, ...] = ()
    from_template: bool = False  # True si se redactó sin LLM


class PreguntaSeguimiento(Payload):
    """Agente 1 (recepción). Como máximo una por reporte (diseño §10).

    `key` es una pregunta del catálogo (agents/recepcion.py) o "libre" si la redactó el
    LLM. `options` va solo con `kind="opciones"`; "No sé" no va en las opciones porque
    siempre está disponible. El evento lleva el `reporte_normalizado` en `refs[0]`.
    """

    report_id: str = Field(pattern=REPORT_ID_PATTERN)
    key: QuestionKey
    kind: QuestionKind
    text: str = Field(min_length=1)
    options: tuple[str, ...] = ()
    reason: str = Field(min_length=1)  # por qué se pregunta: se ve en el tablero
    source: Literal["reglas", "agente"]
    llm: str | None = None  # qué modelo la decidió, p. ej. "gemini/…"; None con reglas

    @model_validator(mode="after")
    def _consistent(self) -> PreguntaSeguimiento:
        if (self.kind == "opciones") != bool(self.options):
            raise ValueError("options va solo, y siempre, con kind opciones")
        if self.options and not 2 <= len(self.options) <= MAX_OPTIONS:
            raise ValueError(f"una pregunta de opciones lleva de 2 a {MAX_OPTIONS} opciones")
        if any(not o.strip() for o in self.options) or len(set(self.options)) != len(self.options):
            raise ValueError("las opciones no pueden estar vacías ni repetirse")
        if self.key == "libre" and self.source != "agente":
            raise ValueError("solo el agente redacta preguntas libres")
        if (self.source == "agente") != (self.llm is not None):
            raise ValueError("llm va solo, y siempre, con source agente")
        return self


# --- Decisiones humanas: vienen del tablero ---------------------------------------------


class Decision(Payload):
    """Aprobar, editar o rechazar una ficha. El evento lleva la ficha en `refs[0]`."""

    action: Literal["aprobar", "editar", "rechazar"]
    edited_message: str | None = None
    operator: str = Field(min_length=1)

    @model_validator(mode="after")
    def _edit_needs_message(self) -> Decision:
        if (self.action == "editar") != (self.edited_message is not None):
            raise ValueError("edited_message va solo, y siempre, con la acción editar")
        return self


CATALOG: dict[str, tuple[EventCategory, type[Payload]]] = {
    "reporte": (EventCategory.HECHO, Reporte),
    "a_salvo": (EventCategory.HECHO, ASalvo),
    "transcripcion_radio": (EventCategory.HECHO, TranscripcionRadio),
    "respuesta_seguimiento": (EventCategory.HECHO, RespuestaSeguimiento),
    "confirmacion_campo": (EventCategory.HECHO, ConfirmacionCampo),
    "reasignacion": (EventCategory.HECHO, Reasignacion),
    "reporte_normalizado": (EventCategory.DERIVADO, ReporteNormalizado),
    "urgencia_estimada": (EventCategory.DERIVADO, UrgenciaEstimada),
    "pregunta_seguimiento": (EventCategory.DERIVADO, PreguntaSeguimiento),
    "incidente_agrupado": (EventCategory.DERIVADO, IncidenteAgrupado),
    "ficha": (EventCategory.DERIVADO, Ficha),
    "decision": (EventCategory.DECISION, Decision),
}


class EventTypeError(ValueError):
    """El evento no corresponde a ningún tipo del catálogo o su payload no es válido."""


def validate_event(event: Event) -> Payload:
    """Devuelve el payload tipado. Lanza EventTypeError si el evento no cumple el catálogo."""
    entry = CATALOG.get(event.type)
    if entry is None:
        raise EventTypeError(f"tipo de evento desconocido: {event.type!r}")
    category, model = entry
    if event.category != category:
        raise EventTypeError(f"{event.type} debe ser {category.value}, no {event.category.value}")
    if event.type in ("pregunta_seguimiento", "respuesta_seguimiento") and not event.refs:
        raise EventTypeError(f"{event.type} debe referenciar el evento en el que se basa")
    if event.type == "decision" and not event.refs:
        raise EventTypeError("una decisión debe referenciar la ficha en refs[0]")
    try:
        payload = model.model_validate(event.payload)
    except ValidationError as exc:
        raise EventTypeError(f"payload inválido para {event.type}: {exc}") from exc
    if isinstance(payload, Reporte) and payload.channel == "vhf" and not event.refs:
        raise EventTypeError("un reporte de radio debe referenciar su transcripción en refs[0]")
    if isinstance(payload, RespuestaSeguimiento):
        node, seq = payload.question_id.split(":")
        if event.refs[:1] != ((node, int(seq)),):
            raise EventTypeError("una respuesta debe referenciar su pregunta en refs[0]")
    if isinstance(payload, Reasignacion):
        if payload.from_node == event.origin_node:
            raise EventTypeError("un nodo no puede reasignarse su propio trabajo")
        if event.refs[:1] != ((payload.from_node, payload.up_to_seq),):
            raise EventTypeError("una reasignación debe referenciar (from_node, up_to_seq)")
    return payload
