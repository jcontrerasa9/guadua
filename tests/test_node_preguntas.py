"""Preguntas de seguimiento en el nodo (D5a): máximo una por reporte, visible en el tablero,
y la respuesta con un toque desde la PWA (formato de docs/contratos.md)."""

import asyncio
import json
import shutil
import subprocess
import threading
import warnings
from pathlib import Path

import pytest

from guadua.node.app import create_app
from guadua.node.config import ConfigError, NodeConfig
from guadua.node.intake import IntakeError, parse_answer
from guadua.node.service import Node
from guadua.node.views import incident_detail, pending_questions
from guadua.protocol.header import Flag, Header, Incident, MessageType, People, encode
from guadua.protocol.text import encode_text
from guadua.providers.llm import LLMReply
from guadua.services.resilience import Work, pending_work
from guadua.store.events import Event, EventCategory
from guadua.store.sqlite import SQLiteEventStore
from tests.fakes import ScriptedLLM
from tests.test_node_cluster import cluster, sync

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

NOW = 1_794_573_600.0
DEVICE = 0xA1B2C3D4
ROOT = Path(__file__).resolve().parents[1]


def report(
    incident: Incident = Incident.COLAPSO,
    flags: Flag = Flag.ATRAPADOS | Flag.OBSERVACION_DIRECTA,
    text: str = "se cayo la casa",
    seq: int = 7,
) -> bytes:
    header = Header(MessageType.REPORTE, DEVICE, seq, 3600, incident, People.DE_2_A_5, flags,
                    barrio=3)  # fmt: skip
    return encode(header) + encode_text(text)


def answer(question_id: str, code: int = 1, *, device: int = DEVICE, seq: int = 100) -> bytes:
    """Igual que `armarRespuesta` de citizen/questions.js (D5b)."""
    header = Header(MessageType.RESPUESTA, device, seq, 3700, Incident.OTRO, People.UNA,
                    Flag.OBSERVACION_DIRECTA, barrio=0)  # fmt: skip
    return encode(header) + bytes([code]) + question_id.encode()


def node(**kwargs) -> Node:
    return Node("A", SQLiteEventStore(), clock=lambda: NOW, classifier=None, **kwargs)


def of_type(n: Node, kind: str) -> list[Event]:
    return [e for e in n.store.since({}) if e.type == kind]


def api(n: Node) -> TestClient:
    return TestClient(create_app(n, gossip_interval=None))


# --- De punta a punta por HTTP ---------------------------------------------------------------


def test_pregunta_respuesta_y_tablero_por_http() -> None:
    n = node()
    client = api(n)
    assert client.post("/api/messages", content=report()).status_code == 200
    (q,) = client.get("/api/questions/a1b2c3d4").json()["questions"]
    assert q["text"] == "¿Alguien no respira o sangra mucho?" and q["kind"] == "si_no"
    assert q["report_id"] == "a1b2c3d4-7" and q["options"] == []
    assert client.get("/api/questions/00000001").json() == {"questions": []}  # otro celular

    step = _recepcion_step(n)
    assert "«¿Alguien no respira o sangra mucho?»" in step and "esperando respuesta" in step
    assert "(reglas)" in step and "Motivo:" in step

    ack = client.post("/api/messages", content=answer(q["question_id"], 1))
    assert ack.json() == {"report_id": "a1b2c3d4-100", "status": "recibido", "duplicate": False}
    assert client.get("/api/questions/a1b2c3d4").json() == {"questions": []}
    assert "dato del ciudadano: alguien no respira o sangra mucho" in _recepcion_step(n)
    (event,) = of_type(n, "respuesta_seguimiento")
    assert event.refs == (("A", int(q["question_id"].split(":")[1])),)
    assert event.payload == {"question_id": q["question_id"], "answer": "si", "option": None,
                             "text": None}  # fmt: skip

    again = client.post("/api/messages", content=answer(q["question_id"], 0, seq=101))
    assert again.json()["duplicate"] is True  # vale la primera respuesta
    assert len(of_type(n, "respuesta_seguimiento")) == 1


def _recepcion_step(n: Node) -> str:
    detail = incident_detail(n.store.since({}), "a1b2c3d4-7")
    (step,) = [s.detail for s in detail.steps if s.agent == "recepcion"]
    return step


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (lambda qid: answer(qid, device=0x01020304), "no es de este dispositivo"),
        (lambda qid: answer(qid, 0x10), "respuesta 'opcion'"),
        (lambda qid: answer(qid, 0x7F), "código de respuesta desconocido"),
        (lambda qid: answer("A:1"), "no es una pregunta"),
        (lambda qid: answer("q-1"), "question_id inválido"),
        (lambda qid: answer(qid)[:21], "respuesta vacía"),
    ],
)
def test_respuestas_invalidas_dan_400(data, error: str) -> None:
    n = node()
    client = api(n)
    client.post("/api/messages", content=report())
    qid = client.get("/api/questions/a1b2c3d4").json()["questions"][0]["question_id"]
    response = client.post("/api/messages", content=data(qid))
    assert response.status_code == 400 and error in response.json()["error"]
    assert of_type(n, "respuesta_seguimiento") == []


def test_respuesta_a_una_pregunta_que_aun_no_llega_se_guarda() -> None:
    n = node()
    assert not n.receive_message(answer("B:40")).duplicate
    (event,) = of_type(n, "respuesta_seguimiento")
    assert event.refs == (("B", 40),)


def test_formato_de_la_respuesta() -> None:
    assert parse_answer(b"\x02A:5").answer == "no_se"
    opcion = parse_answer(b"\x13B:12")
    assert (opcion.answer, opcion.option, opcion.question_id) == ("opcion", 3, "B:12")
    texto = parse_answer(b"\x20\x03A:5" + encode_text("hay humo negro"))
    assert (texto.answer, texto.text, texto.question_id) == ("texto", "hay humo negro", "A:5")
    for bad in (b"\x20\x03A:5", b"\x20", b"\x14A:5", b"\x01" + b"A:" + b"1" * 40):
        with pytest.raises(IntakeError):
            parse_answer(bad)


@pytest.mark.skipif(shutil.which("node") is None, reason="sin Node.js")
def test_el_nodo_entiende_la_respuesta_que_arma_la_pwa() -> None:
    """Los bytes de `armarRespuesta` (citizen/questions.js, D5b) tal cual."""
    n = node()
    n.receive_message(report())
    (q,) = pending_questions(n.store.since({}), "a1b2c3d4", now=NOW)
    script = (
        "import { armarRespuesta } from './citizen/questions.js';"
        f"const r = armarRespuesta({json.dumps(q.question_id)}, false,"
        f" {{ deviceId: {DEVICE}, seq: 55, deviceTime: 4000 }});"
        "console.log(Buffer.from(r.fullBytes).toString('hex'));"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=30,
    )  # fmt: skip
    ack = n.receive_message(bytes.fromhex(out.stdout.strip()))
    assert ack.report_id == "a1b2c3d4-55" and not ack.duplicate
    (event,) = of_type(n, "respuesta_seguimiento")
    assert event.payload["answer"] == "no"


# --- Máximo una por reporte ------------------------------------------------------------------


def test_reprocesar_no_duplica_la_pregunta() -> None:
    n = node()
    n.receive_message(report())
    n.receive_message(report())  # el mismo reporte otra vez
    (normalized,) = of_type(n, "reporte_normalizado")
    n._ask(normalized)
    n.do_work(Work("preguntar", normalized, normalized.id))
    assert len(of_type(n, "pregunta_seguimiento")) == 1
    assert [w for w in pending_work(n.store.since({})) if w.kind == "preguntar"] == []


def test_reporte_completo_sos_y_bateria_no_generan_pregunta() -> None:
    n = node()
    full = Flag.ATRAPADOS | Flag.NO_RESPIRA_O_SANGRADO | Flag.RIESGO_ESCENA
    n.receive_message(report(flags=full, text="", seq=1))
    sos = Header(MessageType.SOS_ESCOMBROS, DEVICE, 2, 3600, Incident.ATRAPADOS, People.UNA,
                 Flag.OBSERVACION_DIRECTA | Flag.BATERIA_CRITICA, barrio=3)  # fmt: skip
    n.receive_message(encode(sos))
    n.receive_message(report(flags=Flag.ATRAPADOS | Flag.BATERIA_CRITICA, text="", seq=3))
    assert of_type(n, "pregunta_seguimiento") == []
    assert [w for w in pending_work(n.store.since({})) if w.kind == "preguntar"] == []


def test_con_llm_lento_la_resiliencia_no_pregunta_dos_veces() -> None:
    """Mientras el LLM piensa en otro hilo, una ronda de resiliencia ve el paso pendiente
    pero no lo repite; al volver el LLM, se publica una sola pregunta, la del agente."""
    release = threading.Event()

    class Slow(ScriptedLLM):
        def complete(self, messages, tools):
            release.wait(5)
            return super().complete(messages, tools)

    async def scenario() -> None:
        n = node(recepcion_llm=Slow([LLMReply(final={"key": "atrapados"})]))
        n.receive_message(report(Incident.INCENDIO, Flag.OBSERVACION_DIRECTA, "se quema todo"))
        assert of_type(n, "pregunta_seguimiento") == [] and n.asking
        n.resilience.check()
        release.set()
        while n.agent_tasks:
            await asyncio.sleep(0.01)
        (q,) = of_type(n, "pregunta_seguimiento")
        assert (q.payload["key"], q.payload["source"]) == ("atrapados", "agente")
        assert n.resilience.check() == []

    asyncio.run(scenario())


def test_solo_el_dueno_pregunta_y_se_responde_desde_otro_nodo() -> None:
    async def scenario() -> None:
        nodes = cluster()
        nodes["A"].receive_message(report())
        await sync(nodes)
        for name in "BC":
            questions = of_type(nodes[name], "pregunta_seguimiento")
            assert [e.origin_node for e in questions] == ["A"]
        (q,) = pending_questions(nodes["C"].store.since({}), "a1b2c3d4", now=NOW)  # la PWA lee de C
        nodes["C"].receive_message(answer(q.question_id, 2))
        await sync(nodes)
        assert pending_questions(nodes["A"].store.since({}), "a1b2c3d4", now=NOW) == []
        assert "dato del ciudadano: no sabe" in _recepcion_step(nodes["A"])

    asyncio.run(scenario())


def test_tras_una_particion_vale_la_pregunta_mas_antigua() -> None:
    n = node()
    n.receive_message(report())
    (first,) = of_type(n, "pregunta_seguimiento")
    gas = {"key": "gas_o_cables", "text": "¿Huele a gas o hay cables caídos?"}
    parallel = Event(
        origin_node="B", seq=40, category=EventCategory.DERIVADO, type="pregunta_seguimiento",
        payload={**first.payload, **gas}, refs=first.refs, received_at=NOW + 5,
    )  # fmt: skip
    n.store.append(parallel)
    (q,) = pending_questions(n.store.since({}), "a1b2c3d4", now=NOW)
    assert q.question_id == f"A:{first.seq}"
    n.receive_message(answer("B:40", 1))  # la PWA respondió la paralela: también cuenta
    assert pending_questions(n.store.since({}), "a1b2c3d4", now=NOW) == []
    step = _recepcion_step(n)
    assert "¿Alguien no respira" in step and "respondió Sí" in step


# --- Configuración ----------------------------------------------------------------------------


def test_tipos_de_pregunta_desde_el_entorno() -> None:
    assert NodeConfig.from_env({}).question_kinds == ("si_no",)
    config = NodeConfig.from_env({"SARD_QUESTION_KINDS": "si_no, opciones,texto"})
    assert config.question_kinds == ("si_no", "opciones", "texto")
    for bad in ("audio", ","):
        with pytest.raises(ConfigError):
            NodeConfig.from_env({"SARD_QUESTION_KINDS": bad})


def test_con_opciones_activas_la_api_las_entrega() -> None:
    n = node(question_kinds=frozenset({"si_no", "opciones"}))
    n.receive_message(report(Incident.VIA_BLOQUEADA, Flag.OBSERVACION_DIRECTA, ""))
    assert of_type(n, "pregunta_seguimiento") == []  # nada que preguntar
    header = Header(MessageType.REPORTE, DEVICE, 8, 3600, Incident.HERIDO, People.NO_SE,
                    Flag.OBSERVACION_DIRECTA, barrio=3)  # fmt: skip
    n.receive_message(encode(header) + encode_text("el señor sangra mucho"))
    (q,) = pending_questions(n.store.since({}), "a1b2c3d4", now=NOW)
    assert q.kind == "opciones" and q.options == ("1", "2 a 5", "6 a 20", "Más de 20")
    n.receive_message(answer(q.question_id, 0x12))
    assert "dato del ciudadano: 6 a 20 personas" in [
        s.detail for s in incident_detail(n.store.since({}), "a1b2c3d4-8").steps
        if s.agent == "recepcion"
    ][0]  # fmt: skip


def test_la_pregunta_mas_reciente_primero_y_solo_por_5_minutos() -> None:
    """La PWA muestra la primera: debe ser la del último reporte, no la de uno viejo."""
    clock = [NOW]
    n = Node("A", SQLiteEventStore(), clock=lambda: clock[0], classifier=None)
    n.receive_message(report(seq=1))
    clock[0] += 120
    n.receive_message(report(Incident.INCENDIO, Flag.INCENDIO | Flag.OBSERVACION_DIRECTA,
                             "se quema la casa", seq=2))  # fmt: skip
    events = n.store.since({})
    assert [q.report_id for q in pending_questions(events, "a1b2c3d4", now=clock[0])] == [
        "a1b2c3d4-2", "a1b2c3d4-1",
    ]  # fmt: skip
    later = NOW + 301  # el primero ya pasó los 5 min; el segundo no
    assert [q.report_id for q in pending_questions(events, "a1b2c3d4", now=later)] == ["a1b2c3d4-2"]
    assert pending_questions(events, "a1b2c3d4", now=NOW + 120 + 301) == []
    client = api(n)  # la ruta usa el reloj del nodo
    clock[0] = NOW + 301
    (q,) = client.get("/api/questions/a1b2c3d4").json()["questions"]
    assert q["report_id"] == "a1b2c3d4-2"
    old = f"A:{[e for e in events if e.type == 'pregunta_seguimiento'][0].seq}"
    assert not n.receive_message(answer(old, 1)).duplicate  # vencida, pero se acepta
