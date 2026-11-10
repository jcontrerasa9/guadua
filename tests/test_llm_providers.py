"""Clientes Gemini y Ollama, protocolo JSON y cadena de proveedores. Sin red: MockTransport."""

import json

import httpx
import pytest

from guadua.providers.fallback import FallbackLLM, LLMConfigError, llm_from_env
from guadua.providers.http_llm import GeminiLLM, OllamaLLM
from guadua.providers.json_protocol import TOOL_RESULT_PREFIX, parse_reply
from guadua.providers.llm import LLMProvider, LLMReply, LLMUnavailable, Message, ToolSpec
from tests.fakes import NullLLM, ScriptedLLM

TOOLS = (ToolSpec("incidentes_cercanos", "Incidentes cerca.", {"type": "object"}),)
MESSAGES = (
    Message("system", "Eres el agente de triaje."),
    Message("user", "Reporte: humo en Chipre"),
    Message("assistant", '{"tool": "incidentes_cercanos", "args": {}}'),
    Message("tool", "[]"),
)


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- Protocolo JSON ----------------------------------------------------------------------


def test_parse_reply_herramienta_y_final() -> None:
    assert parse_reply('{"tool": "x", "args": {"a": 1}}') == LLMReply("x", {"a": 1})
    assert parse_reply('{"tool": "x"}') == LLMReply("x", {})
    assert parse_reply('{"final": {"level": 2}}') == LLMReply(final={"level": 2})


@pytest.mark.parametrize(
    "text",
    ["no es json", "[1, 2]", '{"otra": 1}', '{"tool": 3}', '{"tool": "x", "args": [1]}',
     '{"final": "texto"}', '{"tool": "x", "final": {}}'],
)  # fmt: skip
def test_parse_reply_rechaza_lo_que_no_cumple(text: str) -> None:
    with pytest.raises(LLMUnavailable):
        parse_reply(text)


# --- Gemini -------------------------------------------------------------------------------


def gemini_ok(seen: list[httpx.Request], text: str = '{"final": {"level": 3}}'):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        return httpx.Response(200, json=body)

    return handler


def test_gemini_arma_la_peticion_y_lee_la_respuesta() -> None:
    seen: list[httpx.Request] = []
    llm = GeminiLLM("clave-de-prueba", "modelo-x", client=client(gemini_ok(seen)))
    assert isinstance(llm, LLMProvider)
    assert llm.complete(MESSAGES, TOOLS) == LLMReply(final={"level": 3})
    (request,) = seen
    assert request.url.path.endswith("/models/modelo-x:generateContent")
    assert request.headers["x-goog-api-key"] == "clave-de-prueba"
    assert "clave" not in str(request.url)  # la clave nunca va en la URL
    body = json.loads(request.content)
    system = body["systemInstruction"]["parts"][0]["text"]
    assert "Eres el agente de triaje." in system and "incidentes_cercanos" in system
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["contents"][2]["parts"][0]["text"] == TOOL_RESULT_PREFIX + "[]"
    assert body["generationConfig"]["responseMimeType"] == "application/json"


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(429, json={"error": "cuota"}),
        lambda r: httpx.Response(200, json={"candidates": []}),
        lambda r: httpx.Response(200, text="<html>"),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("sin red")),
    ],
)
def test_gemini_cualquier_falla_es_llm_unavailable(handler) -> None:
    llm = GeminiLLM("k", "m", client=client(handler))
    with pytest.raises(LLMUnavailable):
        llm.complete(MESSAGES, TOOLS)


def test_gemini_sin_clave_o_modelo_no_se_crea() -> None:
    with pytest.raises(ValueError):
        GeminiLLM("", "m")
    with pytest.raises(ValueError):
        GeminiLLM("k", "")


# --- Ollama -------------------------------------------------------------------------------


def test_ollama_arma_la_peticion_y_lee_la_respuesta() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"message": {"content": '{"tool": "x", "args": {}}'}})

    llm = OllamaLLM("qwen-prueba", host="http://ollama:11434/", client=client(handler))
    assert llm.complete(MESSAGES, TOOLS) == LLMReply("x", {})
    (request,) = seen
    assert str(request.url) == "http://ollama:11434/api/chat"
    body = json.loads(request.content)
    assert body["model"] == "qwen-prueba" and body["format"] == "json"
    assert body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "user"]


def test_ollama_caido_es_llm_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conexión rechazada")

    with pytest.raises(LLMUnavailable):
        OllamaLLM("m", client=client(handler)).complete(MESSAGES, TOOLS)


# --- Cadena ------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_la_cadena_cae_al_siguiente_y_dice_cual_respondio() -> None:
    ok = ScriptedLLM([LLMReply(final={"level": 2})])
    chain = FallbackLLM([NullLLM(), ok])
    assert chain.complete(MESSAGES, TOOLS) == LLMReply(final={"level": 2})
    assert chain.last_used == "scripted"


def test_la_cadena_sin_ninguno_lanza_llm_unavailable() -> None:
    chain = FallbackLLM([NullLLM(), NullLLM()])
    with pytest.raises(LLMUnavailable, match="ningún LLM"):
        chain.complete(MESSAGES, TOOLS)
    assert chain.last_used is None


def test_tras_una_falla_el_proveedor_espera_antes_de_reintentarse() -> None:
    calls = []

    class Flaky:
        name = "nube"

        def complete(self, messages, tools):
            calls.append(1)
            raise LLMUnavailable("sin red")

    clock = Clock()
    local = ScriptedLLM([LLMReply(final={})] * 3)
    chain = FallbackLLM([Flaky(), local], cooldown_s=60, clock=clock)
    chain.complete(MESSAGES, TOOLS)
    chain.complete(MESSAGES, TOOLS)  # no vuelve a esperar a la nube
    assert len(calls) == 1
    clock.now = 61
    chain.complete(MESSAGES, TOOLS)
    assert len(calls) == 2


# --- Configuración ------------------------------------------------------------------------


def test_sin_variables_no_hay_llm() -> None:
    assert llm_from_env({}) is None
    assert llm_from_env({"LLM_PROVIDER": "none", "OLLAMA_MODEL": "m"}) is None
    assert llm_from_env({"GEMINI_API_KEY": "k"}) is None  # sin modelo no se usa


def test_auto_pone_la_nube_primero_y_luego_el_local() -> None:
    env = {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "g", "OLLAMA_MODEL": "o"}
    chain = llm_from_env(env)
    assert [p.name for p in chain.providers] == ["gemini", "ollama"]
    only_local = llm_from_env({"OLLAMA_MODEL": "o", "OLLAMA_HOST": "http://h:1"})
    assert [p.name for p in only_local.providers] == ["ollama"]


def test_un_proveedor_pedido_y_sin_configurar_es_error() -> None:
    with pytest.raises(LLMConfigError):
        llm_from_env({"LLM_PROVIDER": "gemini"})
    with pytest.raises(LLMConfigError):
        llm_from_env({"LLM_PROVIDER": "ollama"})
    with pytest.raises(LLMConfigError):
        llm_from_env({"LLM_PROVIDER": "chatgpt"})
