"""Radio VHF (D7): un clip con dos incidentes → dos reportes ligados al mismo audio.

Sin Whisper ni audio real: transcriptor simulado (`FakeTranscriber`) y LLM simulado.
"""

import asyncio
import warnings
from pathlib import Path

import pytest

from guadua.agents.recepcion_radio import (
    InvalidSplit,
    SeparadorRadio,
    SplitCase,
    split_rules,
    validate_split,
)
from guadua.agents.triaje import ToolError, TriageCase, radio_en_zona
from guadua.node import app as app_module
from guadua.node.app import create_app
from guadua.node.pipeline import CITY_GEOHASH, CITY_RADIUS_M
from guadua.node.service import Node
from guadua.node.views import incident_details
from guadua.protocol.header import Flag, Header, Incident, MessageType, People, encode
from guadua.protocol.text import encode_text
from guadua.providers.llm import LLMReply
from guadua.providers.transcriber import (
    NullTranscriber,
    TranscriberConfigError,
    transcriber_from_env,
)
from guadua.radio.clips import DirClipStore, audio_id_of
from guadua.radio.texto import direct_cue, people_count
from guadua.services.resilience import pending_work
from guadua.store.event_types import (
    ReporteNormalizado,
    TranscripcionRadio,
    UrgenciaEstimada,
    validate_event,
)
from guadua.store.events import Event, EventCategory
from guadua.store.sqlite import SQLiteEventStore
from tests.fakes import FakeTranscriber, NullLLM, ScriptedLLM
from tests.test_node_cluster import cluster, sync

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

NOW = 1_794_573_600.0
TWO = (
    "Central, estamos en el sitio, incendio en la 23 del Centro. "
    "Nos comentan que hay gente atrapada en El Cable. Cambio."
)
ONE = "Central, llegamos al sitio en Milán. Tenemos un herido con fractura. Cambio."
NO_PLACE = "Central, la comunidad dice que hay un deslizamiento que tapó la vía. Cambio."
CLIP_TWO, CLIP_ONE, CLIP_NO_PLACE, NOISE = b"clip-dos", b"clip-uno", b"clip-sin-lugar", b"ruido"
WAV = {"Content-Type": "audio/wav"}


def node(llm=None, transcriber=None) -> Node:
    texts = {CLIP_TWO: TWO, CLIP_ONE: ONE, CLIP_NO_PLACE: NO_PLACE}
    return Node(
        "A",
        SQLiteEventStore(),
        clock=lambda: NOW,
        classifier=None,
        recepcion_llm=llm,
        transcriber=transcriber if transcriber is not None else FakeTranscriber(texts),
    )


def api(n: Node) -> TestClient:
    return TestClient(create_app(n, gossip_interval=None))


def of_type(n: Node, kind: str) -> list[Event]:
    return [e for e in n.store.since({}) if e.type == kind]


def radio_reports(n: Node) -> list[Event]:
    return [e for e in of_type(n, "reporte") if e.payload["channel"] == "vhf"]


def final(*parts: dict) -> LLMReply:
    return LLMReply(final={"partes": list(parts)})


INCENDIO = {"texto": "estamos en el sitio, incendio en la 23 del Centro", "tipo": "incendio",
            "barrio": "Centro", "directo": True}  # fmt: skip
ATRAPADOS = {"texto": "Nos comentan que hay gente atrapada en El Cable", "tipo": "atrapados",
             "barrio": "El Cable", "directo": False}  # fmt: skip


# --- Terminado cuando: dos incidentes, dos reportes, el mismo audio ----------------------------


def test_clip_con_dos_incidentes_da_dos_reportes_ligados_al_mismo_audio() -> None:
    n = node()
    client = api(n)
    res = client.post("/api/radio", content=CLIP_TWO, headers=WAV)
    assert res.status_code == 200
    ack = res.json()
    assert ack["status"] == "transcrito" and ack["text"] == TWO and ack["duplicate"] is False
    (transcription,) = of_type(n, "transcripcion_radio")
    assert ack["transcription_id"] == "A:1" and ack["audio_id"] == audio_id_of(CLIP_TWO)

    first, second = radio_reports(n)
    for r in (first, second):
        assert r.payload["audio_id"] == ack["audio_id"]  # ligados al mismo audio
        assert r.refs == (transcription.id,)
        assert r.payload["raw_hex"] == "" and r.payload["split_by"] == "reglas"
        validate_event(r)
    device = first.payload["report_id"].split("-")[0]
    assert (first.payload["report_id"], second.payload["report_id"]) == (
        f"{device}-1",
        f"{device}-2",
    )
    assert first.payload["incident"] == Incident.INCENDIO and first.payload["barrio"] == 4
    assert second.payload["incident"] == Incident.ATRAPADOS and second.payload["barrio"] == 1
    assert Flag.OBSERVACION_DIRECTA in Flag(first.payload["flags"])
    assert Flag.OBSERVACION_DIRECTA not in Flag(second.payload["flags"])

    # Cada uno sigue la cadena de siempre y es su propio incidente en el tablero.
    assert len(of_type(n, "reporte_normalizado")) == len(of_type(n, "urgencia_estimada")) == 2
    cards = {c["title"]: c for c in client.get("/api/incidents").json()["incidents"]}
    assert set(cards) == {"Incendio", "Personas atrapadas"}
    detail = client.get(f"/api/incidents/{cards['Personas atrapadas']['incident_id']}").json()
    assert detail["sources"] == ["radio de segunda mano"]
    assert any(f"clip {ack['audio_id']} · parte 2" in s["detail"] for s in detail["steps"])
    clips = client.get("/api/radio").json()["clips"]
    assert clips[0]["report_ids"] == [first.payload["report_id"], second.payload["report_id"]]
    assert clips[0]["has_audio"] and clips[0]["source"] == "whisper"


def test_el_piso_sale_del_texto_de_cada_parte() -> None:
    n = node()
    asyncio.run(n.receive_audio(CLIP_TWO, "audio/wav"))
    for urgency in of_type(n, "urgencia_estimada"):
        u = UrgenciaEstimada.model_validate(urgency.payload)
        assert u.floor == u.level == 4  # incendio y atrapados: crítica, también por radio


def test_por_radio_no_hay_preguntas_de_seguimiento() -> None:
    n = node()
    asyncio.run(n.receive_audio(CLIP_TWO, "audio/wav"))
    assert of_type(n, "pregunta_seguimiento") == []
    assert not [w for w in pending_work(n.store.since({})) if w.kind == "preguntar"]


def test_lo_que_la_radio_no_dice_queda_desconocido() -> None:
    n = node()
    asyncio.run(n.receive_audio(CLIP_NO_PLACE, "audio/ogg"))
    (normalized,) = of_type(n, "reporte_normalizado")
    report = ReporteNormalizado.model_validate(normalized.payload)
    assert report.location.geohash == CITY_GEOHASH and report.location.radius_m == CITY_RADIUS_M
    assert {"location", "people"} <= set(report.unknown_fields)
    assert report.direct_observation is False  # "la comunidad dice": segunda mano


# --- Entrada del audio -------------------------------------------------------------------------


def test_el_mismo_clip_dos_veces_es_duplicado() -> None:
    transcriber = FakeTranscriber({CLIP_TWO: TWO})
    n = node(transcriber=transcriber)
    client = api(n)
    client.post("/api/radio", content=CLIP_TWO, headers=WAV)
    again = client.post("/api/radio", content=CLIP_TWO, headers=WAV).json()
    assert again["duplicate"] is True and again["transcription_id"] == "A:1"
    assert transcriber.calls == 1 and len(of_type(n, "transcripcion_radio")) == 1
    assert len(radio_reports(n)) == 2


@pytest.mark.parametrize(
    ("body", "headers", "fragment"),
    [
        (CLIP_TWO, {"Content-Type": "text/plain"}, "tipo de audio"),
        (b"", WAV, "vacío"),
        (b"x" * (8 * 1024 * 1024 + 1), WAV, "MB"),
    ],
)
def test_audio_invalido_es_400(body: bytes, headers: dict, fragment: str) -> None:
    res = api(node()).post("/api/radio", content=body, headers=headers)
    assert res.status_code == 400 and fragment in res.json()["error"]


def test_el_nodo_sirve_el_audio_que_recibio() -> None:
    client = api(node())
    audio_id = client.post("/api/radio", content=CLIP_ONE, headers=WAV).json()["audio_id"]
    res = client.get(f"/api/radio/{audio_id}/audio")
    assert res.status_code == 200 and res.content == CLIP_ONE
    assert res.headers["content-type"] == "audio/wav"
    assert client.get("/api/radio/0123456789abcdef/audio").status_code == 404
    assert client.get("/api/radio/no-es-un-id/audio").status_code == 404


def test_clip_de_demostracion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "clip_demo.ogg").write_bytes(CLIP_TWO)
    monkeypatch.setattr(app_module, "RADIO_DIR", tmp_path)
    client = api(node())
    ack = client.post("/api/radio/demo/clip_demo").json()
    assert ack["status"] == "transcrito" and ack["text"] == TWO
    assert client.get("/api/radio").json()["clips"][0]["name"] == "clip_demo"
    assert client.post("/api/radio/demo/no_existe").status_code == 404
    assert client.post("/api/radio/demo/..%2Fsecreto").status_code == 404


# --- Respaldo manual: sin transcriptor, audio ininteligible y correcciones ----------------------


def test_sin_transcriptor_el_operador_escribe_el_texto() -> None:
    n = node(transcriber=NullTranscriber())
    client = api(n)
    ack = client.post("/api/radio", content=NOISE, headers=WAV).json()
    assert ack["status"] == "pendiente" and "operador" in ack["note"]
    assert of_type(n, "transcripcion_radio") == []
    (clip,) = client.get("/api/radio").json()["clips"]
    assert clip["status"] == "pendiente" and clip["text"] is None and clip["has_audio"]

    res = client.post(
        f"/api/radio/{ack['audio_id']}/transcription",
        json={"text": ONE, "operator": "comandante-1"},
    )
    assert res.status_code == 201 and res.json()["status"] == "transcrito"
    (t,) = of_type(n, "transcripcion_radio")
    assert t.refs == () and t.payload["source"] == "operador"
    assert t.payload["operator"] == "comandante-1"
    assert len(radio_reports(n)) == 1


def test_audio_ininteligible_queda_pendiente() -> None:
    ack = api(node()).post("/api/radio", content=NOISE, headers=WAV).json()
    assert ack["status"] == "pendiente" and ack["note"] == "audio ininteligible"


def test_una_correccion_es_un_evento_nuevo_y_reemplaza_los_reportes() -> None:
    n = node()
    client = api(n)
    audio_id = client.post("/api/radio", content=CLIP_TWO, headers=WAV).json()["audio_id"]
    before = {e.id: e for e in n.store.since({})}
    fixed = TWO.replace("El Cable", "Chipre")
    res = client.post(
        f"/api/radio/{audio_id}/transcription", json={"text": fixed, "operator": "c-1"}
    )
    assert res.status_code == 201
    old, new = of_type(n, "transcripcion_radio")
    assert new.refs == (old.id,) and new.payload["text"] == fixed
    for event_id, event in before.items():  # inmutables: nada se editó ni se borró
        assert n.store.get(event_id) == event
    assert len(radio_reports(n)) == 4  # los dos viejos siguen en el registro
    titles = {d.location.label for d in incident_details(n.store.since({}))}
    assert titles == {"Centro", "Chipre"}  # el tablero muestra solo los de la corrección
    (clip,) = client.get("/api/radio").json()["clips"]
    assert clip["text"] == fixed and clip["corrections"] == 1 and clip["operator"] == "c-1"
    assert not [w for w in pending_work(n.store.since({})) if w.kind == "separar"]


def test_corregir_un_clip_desconocido_es_404() -> None:
    client = api(node())
    body = {"text": "hola", "operator": "c-1"}
    assert client.post("/api/radio/0123456789abcdef/transcription", json=body).status_code == 404
    assert client.post("/api/radio/abc/transcription", json=body).status_code == 404
    bad = client.post("/api/radio/0123456789abcdef/transcription", json={"text": ""})
    assert bad.status_code == 400


# --- Separación con y sin LLM -----------------------------------------------------------------


def _case(text: str) -> SplitCase:
    payload = TranscripcionRadio(audio_id="0" * 16, text=text, direct=False)
    event = Event(origin_node="A", seq=1, category=EventCategory.HECHO,
                  type="transcripcion_radio", payload=payload.model_dump(mode="json"),
                  received_at=NOW)  # fmt: skip
    return SplitCase(event, payload)


def test_con_llm_valido_separa_el_agente() -> None:
    n = node(llm=ScriptedLLM([final(INCENDIO, ATRAPADOS)]))
    asyncio.run(n.receive_audio(CLIP_TWO, "audio/wav"))
    first, second = radio_reports(n)
    assert first.payload["split_by"] == second.payload["split_by"] == "scripted"
    assert second.payload["text"] == ATRAPADOS["texto"]


class Spy(ScriptedLLM):
    def __init__(self, replies) -> None:
        super().__init__(replies)
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        return super().complete(messages, tools)


def test_un_solo_suceso_no_gasta_llamadas_al_llm() -> None:
    spy = Spy([])
    split = SeparadorRadio(spy).run(_case(ONE))
    assert spy.calls == 0 and split.split_by == "reglas" and len(split.parts) == 1


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        (final(INCENDIO), "se perdería"),  # omitió los atrapados: el piso bajaría
        (final({**INCENDIO, "texto": "incendio grande en el Centro"}, ATRAPADOS), "no está"),
        (final(INCENDIO, {**ATRAPADOS, "barrio": "Palermo"}), "barrio"),
        (final(INCENDIO, {**ATRAPADOS, "tipo": "tsunami"}), "tipo"),
        (final(*[INCENDIO] * 5), "partes"),
        (LLMReply(tool_name="buscar_lugar"), "herramienta"),
    ],
)
def test_lo_invalido_del_llm_cae_a_las_reglas(reply: LLMReply, why: str) -> None:
    split = SeparadorRadio(ScriptedLLM([reply])).run(_case(TWO))
    assert split.split_by == "reglas" and why in split.note
    assert [p.incident for p in split.parts] == [Incident.INCENDIO, Incident.ATRAPADOS]


def test_sin_llm_que_responda_quedan_las_reglas() -> None:
    split = SeparadorRadio(NullLLM()).run(_case(TWO))
    assert split.split_by == "reglas" and len(split.parts) == 2


def test_el_llm_no_puede_inventar_que_lo_vieron() -> None:
    text = "Nos dicen que hay un incendio en Palermo y otro incendio en Milán."
    parts = [{"texto": "Nos dicen que hay un incendio en Palermo", "tipo": "incendio",
              "barrio": "Palermo", "directo": True},
             {"texto": "otro incendio en Milán", "tipo": "incendio", "barrio": "Milán",
              "directo": None}]  # fmt: skip
    with pytest.raises(InvalidSplit, match="directo"):
        validate_split({"partes": parts}, text)


def test_las_personas_salen_del_texto_no_del_llm() -> None:
    (part,) = validate_split(
        {"partes": [{"texto": "colapso en Milán con tres atrapados", "tipo": "colapso",
                     "barrio": "Milán", "directo": None, "personas": "mas_de_20"}]},
        "Central, colapso en Milán con tres atrapados.",
    )  # fmt: skip
    assert part.people == People.DE_2_A_5


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (TWO, [("INCENDIO", "Centro", True), ("ATRAPADOS", "El Cable", False)]),
        ("Incendio en Chipre, hay dos personas atrapadas en el segundo piso.",
         [("INCENDIO", "Chipre", None)]),
        ("Colapso en Palermo con tres atrapados y además una fuga de gas en Milán.",
         [("COLAPSO", "Palermo", None), ("FUGA_GAS", "Milán", None)]),
        ("Estamos en Palermo con el incendio. La comunidad dice que en Milán también hay una "
         "casa en llamas.", [("INCENDIO", "Palermo", True), ("INCENDIO", "Milán", False)]),
        ("Nos dicen que hay un incendio en Palermo y otro incendio en Milán.",
         [("INCENDIO", "Palermo", False), ("INCENDIO", "Milán", False)]),
        ("Hay humo en Chipre. Hay humo en Chipre, repito, humo en Chipre.",
         [("INCENDIO", "Chipre", None)]),
    ],
)  # fmt: skip
def test_reglas_de_separacion(text: str, expected: list) -> None:
    got = [(p.incident.name, p.barrio.name if p.barrio else None, p.direct)
           for p in split_rules(text)]  # fmt: skip
    assert got == expected


def test_maximo_cuatro_partes() -> None:
    text = ". ".join(f"Además incendio en {b}" for b in
                     ("El Cable", "Chipre", "Centro", "Palermo", "Milán"))  # fmt: skip
    parts = split_rules(text)
    assert len(parts) == 4 and "Milán" in parts[-1].text


@pytest.mark.parametrize(
    ("text", "direct"),
    [
        ("Estamos en el sitio del incendio", True),
        ("Nos comentan que hay un incendio, estamos llegando", False),
        ("Incendio en Chipre", None),
        ("Confirmamos fuga de gas", True),
    ],
)
def test_directo_o_de_segunda_mano(text: str, direct: bool | None) -> None:
    assert direct_cue(text) is direct


@pytest.mark.parametrize(
    ("text", "people"),
    [("un herido", People.UNA), ("tres personas atrapadas", People.DE_2_A_5),
     ("12 heridos", People.DE_6_A_20), ("un incendio", People.NO_SE),
     ("varias personas", People.NO_SE)],
)  # fmt: skip
def test_personas_dichas_con_numero(text: str, people: People) -> None:
    assert people_count(text) is people


# --- Resiliencia, gossip y triaje -----------------------------------------------------------


def test_una_transcripcion_sin_separar_queda_pendiente_y_se_retoma() -> None:
    n = node()
    payload = TranscripcionRadio(audio_id="0" * 16, text=TWO, direct=True)
    event = Event(origin_node="A", seq=n.next_seq(), category=EventCategory.HECHO,
                  type="transcripcion_radio", payload=payload.model_dump(mode="json"),
                  received_at=NOW)  # fmt: skip
    n.store.append(event)  # como si el nodo cayera antes de separar
    (work,) = [w for w in pending_work(n.store.since({})) if w.kind == "separar"]
    n.do_work(work)
    n.do_work(work)  # repetir no duplica
    assert len(radio_reports(n)) == 2
    assert not [w for w in pending_work(n.store.since({})) if w.kind == "separar"]


def test_solo_el_nodo_que_recibio_el_audio_separa() -> None:
    async def scenario() -> None:
        nodes = cluster()
        nodes["A"].transcriber = FakeTranscriber({CLIP_TWO: TWO})
        await nodes["A"].receive_audio(CLIP_TWO, "audio/wav")
        await sync(nodes)
        for n in nodes.values():
            reports = radio_reports(n)
            assert len(reports) == 2 and {r.origin_node for r in reports} == {"A"}
        assert nodes["C"].clips.clips() == []  # el audio no viaja; la transcripción sí

    asyncio.run(scenario())


def _phone_report(barrio: int) -> bytes:
    header = Header(MessageType.REPORTE, 0xA1B2C3D4, 7, 3600, Incident.OTRO, People.UNA,
                    Flag(0), barrio=barrio)  # fmt: skip
    return encode(header) + encode_text("algo raro pasa")


def _triage_case(n: Node) -> TriageCase:
    urgency = next(e for e in of_type(n, "urgencia_estimada")
                   if e.payload["report_id"] == "a1b2c3d4-7")  # fmt: skip
    normalized = n.store.get(urgency.refs[0])
    return TriageCase(
        urgency,
        UrgenciaEstimada.model_validate(urgency.payload),
        ReporteNormalizado.model_validate(normalized.payload),
        tuple(n.store.since({})),
    )


def test_radio_en_zona_lee_lo_que_dijo_la_radio_en_el_mismo_barrio() -> None:
    n = node()
    asyncio.run(n.receive_audio(CLIP_TWO, "audio/wav"))
    asyncio.run(n.receive_audio(CLIP_NO_PLACE, "audio/wav"))
    n.receive_message(_phone_report(barrio=4))  # Centro
    (heard,) = radio_en_zona(_triage_case(n))
    assert heard["texto"].endswith("incendio en la 23 del Centro")
    assert heard["lo_vieron_en_el_sitio"] is True and heard["tipo"] == "incendio"
    with pytest.raises(ToolError):
        radio_en_zona(_triage_case(n), 5)


def test_radio_en_zona_ignora_reportes_reemplazados() -> None:
    n = node()
    client = api(n)
    audio_id = client.post("/api/radio", content=CLIP_TWO, headers=WAV).json()["audio_id"]
    client.post(f"/api/radio/{audio_id}/transcription",
                json={"text": "Incendio en Palermo.", "operator": "c-1"})  # fmt: skip
    n.receive_message(_phone_report(barrio=4))
    assert radio_en_zona(_triage_case(n)) == []


# --- Transcriptor y almacenamiento ---------------------------------------------------------------


def test_transcriptor_desde_el_entorno(tmp_path: Path) -> None:
    assert isinstance(transcriber_from_env({"SARD_TRANSCRIBER": "none"}), NullTranscriber)
    missing = {"WHISPER_MODEL_DIR": str(tmp_path)}
    assert isinstance(transcriber_from_env(missing), NullTranscriber)  # auto sin modelo
    with pytest.raises(TranscriberConfigError):
        transcriber_from_env({**missing, "SARD_TRANSCRIBER": "whisper"})
    with pytest.raises(TranscriberConfigError):
        transcriber_from_env({"SARD_TRANSCRIBER": "nube"})


def test_los_clips_quedan_en_disco(tmp_path: Path) -> None:
    store = DirClipStore(tmp_path / "audio")
    n = Node("A", SQLiteEventStore(), clock=lambda: NOW, classifier=None,
             transcriber=NullTranscriber(), clips=store)  # fmt: skip
    asyncio.run(n.receive_audio(NOISE, "audio/ogg; codecs=opus"))
    (clip,) = DirClipStore(tmp_path / "audio").clips()  # otro objeto: lo lee del disco
    assert clip.content_type == "audio/ogg" and clip.size == len(NOISE)
    assert store.get(clip.audio_id)[1] == NOISE
    assert store.get("../../etc/passwd") is None


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("application/ogg", "audio/ogg"),  # nota de voz de WhatsApp en algunos navegadores
        ("audio/x-opus+ogg", "audio/ogg"),
        ("video/webm", "audio/webm"),  # grabación del navegador
        ("audio/mpeg", "audio/mpeg"),
    ],
)
def test_tipos_que_dan_los_navegadores(sent: str, stored: str) -> None:
    n = node()
    res = api(n).post("/api/radio", content=CLIP_ONE, headers={"Content-Type": sent})
    assert res.status_code == 200
    (clip,) = n.clips.clips()
    assert clip.content_type == stored
