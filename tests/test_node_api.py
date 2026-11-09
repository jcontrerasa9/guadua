"""Pruebas de la API HTTP del nodo (N6) contra los formatos de node/api.py."""

import json
import warnings
from pathlib import Path

import pytest

from guadua.node.api import SCHEMAS
from guadua.node.app import create_app
from guadua.node.service import Node
from guadua.protocol.header import Flag, Header, MessageType, encode
from guadua.protocol.text import encode_text
from guadua.store.sqlite import SQLiteEventStore

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

EXAMPLES = json.loads((Path(__file__).parent / "fixtures" / "api_examples.json").read_text())
EXAMPLE_KEYS = {case["route"]: set(case["body"]) for case in EXAMPLES["valid"]}

COLAPSO = Header(
    MessageType.REPORTE, 0xA1B2C3D4, 7, 3600, 1, 1,
    Flag.ATRAPADOS | Flag.OBSERVACION_DIRECTA | Flag.GPS | Flag.RIESGO_ESCENA,
    geohash="d2fkcdc",
)  # fmt: skip


def message(header: Header = COLAPSO, text: str = "colapso casa 3 atrapados huele a gas") -> bytes:
    return encode(header) + encode_text(text)


def client(store=None, clock=None) -> TestClient:
    node = Node(
        "A",
        store if store is not None else SQLiteEventStore(),
        clock=clock or (lambda: 1_794_573_600.0),
    )
    return TestClient(create_app(node, gossip_interval=None))


def check(response, schema: str, route: str | None = None) -> dict:
    """La respuesta cumple su modelo y, si hay ejemplo, trae los mismos campos."""
    body = response.json()
    SCHEMAS[schema].model_validate(body)
    if route is not None:
        assert set(body) == EXAMPLE_KEYS[route], f"campos distintos al ejemplo de {route}"
    return body


@pytest.fixture
def api() -> TestClient:
    return client()


# --- Health y mensajes ----------------------------------------------------------------------


def test_health(api: TestClient) -> None:
    body = check(api.get("/api/health"), "Health", "GET /api/health")
    assert body["node"] == "A" and body["peers_alive"] == []


def test_message_ack_and_duplicate(api: TestClient) -> None:
    first = api.post("/api/messages", content=message())
    assert first.status_code == 200
    assert check(first, "MessageAck", "POST /api/messages → 200") == {
        "report_id": "a1b2c3d4-7", "status": "recibido", "duplicate": False,
    }  # fmt: skip
    again = api.post("/api/messages", content=message())
    assert again.status_code == 200 and again.json()["duplicate"] is True
    reportes = [e for e in api.app.state.node.store.since({}) if e.type == "reporte"]
    assert len(reportes) == 1  # el repetido no se guarda dos veces


def test_sos_is_only_the_header(api: TestClient) -> None:
    sos = Header(MessageType.SOS_ESCOMBROS, 0xA1B2C3D4, 8, 3600, 0, 0,
                 Flag.ATRAPADOS | Flag.GPS | Flag.BATERIA_CRITICA, geohash="d2fkcdc")  # fmt: skip
    response = api.post("/api/messages", content=encode(sos))
    assert response.status_code == 200
    reporte = next(e for e in api.app.state.node.store.since({}) if e.type == "reporte")
    assert reporte.payload["text"] is None and reporte.payload["msg_type"] == 1


def test_compressed_text_is_stored_decompressed(api: TestClient) -> None:
    api.post("/api/messages", content=message(text="señora atrapada en Milán"))
    reporte = next(e for e in api.app.state.node.store.since({}) if e.type == "reporte")
    assert reporte.payload["text"] == "señora atrapada en Milán"
    assert reporte.payload["channel"] == "wifi"
    assert reporte.payload["raw_hex"] == message(text="señora atrapada en Milán").hex()


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (encode(COLAPSO)[:20], "al menos 21"),
        (encode(COLAPSO) + b"texto plano", "texto inválido"),
        (encode(Header(4, 1, 1, 0, 6, 0, 0x20, barrio=5)), "no soportado"),
        (b"\x20" + bytes(20), "versión"),
    ],
    ids=["corto", "texto_sin_prefijo", "a_salvo_nivel_2", "version"],
)
def test_bad_messages_are_400(api: TestClient, data: bytes, fragment: str) -> None:
    response = api.post("/api/messages", content=data)
    assert response.status_code == 400
    body = check(response, "ApiError", "POST /api/messages → 400")
    assert fragment in body["error"]
    assert api.app.state.node.store.since({}) == []


# --- Incidentes -----------------------------------------------------------------------------


def test_report_becomes_card_with_pending_ficha(api: TestClient) -> None:
    api.post("/api/messages", content=message())
    body = check(api.get("/api/incidents"), "IncidentList")
    assert set(body) == EXAMPLE_KEYS["GET /api/incidents"]
    (card,) = body["incidents"]
    example_card = EXAMPLES["valid"][3]["body"]["incidents"][0]
    assert set(card) == set(example_card)
    assert card["incident_id"] == "a1b2c3d4-7"
    assert card["level"] == 4  # por la regla de atrapados (N7), ya no provisional
    assert card["status"] == "pendiente" and card["ficha_id"] == "A:5"
    assert card["title"] == "Colapso con 2 a 5 atrapados"
    assert "gas o cables" in card["risk_warning"]
    types = [e.type for e in api.app.state.node.store.since({})]
    assert types == [
        "reporte", "reporte_normalizado", "urgencia_estimada", "incidente_agrupado", "ficha",
    ]  # fmt: skip


def test_incident_detail_and_404(api: TestClient) -> None:
    api.post("/api/messages", content=message())
    detail = check(api.get("/api/incidents/a1b2c3d4-7"), "IncidentDetail")
    assert set(detail) == EXAMPLE_KEYS["GET /api/incidents/a1b2c3d4-7"]
    assert detail["report_ids"] == ["a1b2c3d4-7"]
    assert detail["sources"] == ["ciudadano directo"]
    assert detail["steps"][0]["detail"].startswith("reglas: nivel critica; atrapados")
    assert detail["rules"] == ["atrapados → crítica"]
    assert set(detail["probabilities"]) == {"critica", "alta", "media", "baja"}
    missing = api.get("/api/incidents/no-existe")
    assert missing.status_code == 404
    check(missing, "ApiError")


# --- Decisiones -----------------------------------------------------------------------------


def _decide(api: TestClient, action: str, **extra):
    body = {"ficha_id": "A:5", "action": action, "operator": "comandante-1", **extra}
    return api.post("/api/decisions", json=body)


def test_approve_then_conflict(api: TestClient) -> None:
    api.post("/api/messages", content=message())
    approved = _decide(api, "aprobar")
    assert approved.status_code == 201
    assert check(approved, "DecisionAck", "POST /api/decisions → 201") == {"event_id": "A:6"}
    card = api.get("/api/incidents").json()["incidents"][0]
    assert card["status"] == "aprobada"
    second = _decide(api, "rechazar")
    assert second.status_code == 409
    check(second, "ApiError")


def test_edit_replaces_message(api: TestClient) -> None:
    api.post("/api/messages", content=message())
    assert _decide(api, "editar", edited_message="Enviar 2 unidades ya").status_code == 201
    card = api.get("/api/incidents").json()["incidents"][0]
    assert card["status"] == "editada" and card["message"] == "Enviar 2 unidades ya"


def test_reject_replans_a_new_ficha(api: TestClient) -> None:
    api.post("/api/messages", content=message())
    assert _decide(api, "rechazar").status_code == 201
    card = api.get("/api/incidents").json()["incidents"][0]
    assert card["ficha_id"] != "A:5"  # la coordinación replaneó
    assert card["status"] == "pendiente"
    assert "replaneada: se rechazó Cuerpo Oficial de Bomberos" in card["suggestion"]
    detail = api.get("/api/incidents/a1b2c3d4-7").json()
    rejected = [s["detail"] for s in detail["steps"] if "rechazada por" in s["detail"]]
    assert rejected == ["ficha A:5 (Cuerpo Oficial de Bomberos) rechazada por comandante-1"]


def test_second_rejection_excludes_both(api: TestClient) -> None:
    api.post("/api/messages", content=message())
    _decide(api, "rechazar")
    second = api.get("/api/incidents").json()["incidents"][0]["ficha_id"]
    body = {"ficha_id": second, "action": "rechazar", "operator": "comandante-1"}
    assert api.post("/api/decisions", json=body).status_code == 201
    card = api.get("/api/incidents").json()["incidents"][0]
    assert card["ficha_id"] not in ("A:5", second)
    assert "Cuerpo Oficial de Bomberos" in card["suggestion"].split("se rechazó")[1]
    assert card["suggestion"].count(",") >= 1  # nombra a los dos rechazados


def test_card_without_rejection_is_not_marked_replanned(api: TestClient) -> None:
    api.post("/api/messages", content=message())
    assert "replaneada" not in api.get("/api/incidents").json()["incidents"][0]["suggestion"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"ficha_id": "A:99", "action": "aprobar", "operator": "c"}, 404),
        ({"ficha_id": "A:1", "action": "aprobar", "operator": "c"}, 404),  # es un reporte
        ({"ficha_id": "A:5", "action": "editar", "operator": "c"}, 400),
        ({"ficha_id": "A-4", "action": "aprobar", "operator": "c"}, 400),
        ({"ficha_id": "A:5", "action": "aprobar", "operator": "c", "x": 1}, 400),
    ],
    ids=["no_existe", "no_es_ficha", "editar_sin_mensaje", "id_mal_formado", "campo_extra"],
)
def test_bad_decisions(api: TestClient, body: dict, status: int) -> None:
    api.post("/api/messages", content=message())
    response = api.post("/api/decisions", json=body)
    assert response.status_code == status
    check(response, "ApiError")


# --- Nivel 2, pantallas y CORS --------------------------------------------------------------


def test_level_2_routes_exist_empty(api: TestClient) -> None:
    assert check(api.get("/api/questions/a1b2c3d4"), "QuestionList") == {"questions": []}
    assert check(api.get("/api/people"), "PeopleList") == {"people": []}


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("/tablero/", "<html"),
        ("/ciudadano/", "<html"),
        ("/ciudadano/protocol/text.js", "encodeText"),
        ("/data/barrios.json", "El Cable"),
        ("/data/diccionario_manizales.txt", "Manizales"),
    ],
)
def test_serves_views(api: TestClient, path: str, content: str) -> None:
    response = api.get(path)
    assert response.status_code == 200 and content in response.text


def test_root_redirects_and_unknown_data_is_404(api: TestClient) -> None:
    assert api.get("/", follow_redirects=False).headers["location"] == "/tablero/"
    assert api.get("/data/matriz_emre.csv").status_code == 404
    assert api.get("/data/..%2Fpyproject.toml").status_code == 404


@pytest.mark.parametrize(
    "path", ["/tablero/", "/tablero/logic.js", "/ciudadano/report.js", "/data/barrios.json"]
)
def test_views_are_always_revalidated(api: TestClient, path: str) -> None:
    """Sin esto el navegador puede reusar un tablero viejo tras una actualización."""
    response = api.get(path)
    assert response.headers["cache-control"] == "no-cache"
    again = api.get(path, headers={"If-None-Match": response.headers["etag"]})
    assert again.status_code == 304 or path.startswith("/data/")


def test_api_responses_have_no_view_cache_header(api: TestClient) -> None:
    assert "cache-control" not in api.get("/api/health").headers


def test_cors_allows_other_nodes_dashboard(api: TestClient) -> None:
    response = api.get("/api/health", headers={"Origin": "http://localhost:8001"})
    assert response.headers["access-control-allow-origin"] == "*"


# --- Persistencia ---------------------------------------------------------------------------


def test_restart_keeps_events_and_continues_seq(tmp_path: Path) -> None:
    path = tmp_path / "a.sqlite"
    first = client(SQLiteEventStore(path))
    first.post("/api/messages", content=message())
    first.app.state.node.store.close()

    second = client(SQLiteEventStore(path))
    assert second.post("/api/messages", content=message()).json()["duplicate"] is True
    other = Header(0, 0xA1B2C3D4, 8, 3700, 2, 0, Flag.GPS, geohash="d2fkcdc")
    second.post("/api/messages", content=encode(other))
    seqs = [e.seq for e in second.app.state.node.store.since({}) if e.origin_node == "A"]
    assert seqs == list(range(1, len(seqs) + 1))  # sin huecos ni repetidos tras reiniciar
