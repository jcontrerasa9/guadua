"""Agente de triaje (N8): ciclo en la zona gris con los guardias de la regla 1. LLM simulado."""

import asyncio
import dataclasses
import itertools
import threading

import pytest

from guadua.agents.triaje import (
    MAX_STEPS,
    AgenteTriaje,
    TriageCase,
    decide_level,
    incidentes_cercanos,
    needs_agent,
    quote_in_text,
)
from guadua.classifier.triage import Prediction
from guadua.classifier.urgency import Urgency
from guadua.node.service import Node
from guadua.node.views import incident_cards, incident_detail
from guadua.protocol.header import Flag, Header, Incident, MessageType, encode
from guadua.protocol.text import encode_text
from guadua.providers.fallback import FallbackLLM
from guadua.providers.llm import LLMReply, LLMUnavailable
from guadua.store.event_types import UrgenciaEstimada
from guadua.store.sqlite import SQLiteEventStore
from tests.fakes import NullLLM, ScriptedLLM
from tests.test_node_cluster import cluster, sync

NOW = 1_794_573_600.0
GATO = "hay un gato herido en la calle"
GRAY = {"critica": 0.1, "alta": 0.4, "media": 0.35, "baja": 0.15}


class GrayClassifier:
    """Sugiere alta con dudas: el caso queda en la zona gris (ambiguo)."""

    def predict(self, incident, text) -> Prediction:
        return Prediction(Urgency.ALTA, GRAY)


def message(
    text: str, *, seq: int = 7, barrio: int = 0, flags: Flag = Flag.OBSERVACION_DIRECTA
) -> bytes:
    header = Header(
        MessageType.REPORTE, 0xA1B2C3D4, seq, 3600, Incident.HERIDO, 1, flags, barrio=barrio
    )
    return encode(header) + encode_text(text)


def node(llm=None, name: str = "A") -> Node:
    return Node(name, SQLiteEventStore(), clock=lambda: NOW, classifier=GrayClassifier(), llm=llm)


def urgency_events(n: Node):
    return [e for e in n.store.since({}) if e.type == "urgencia_estimada"]


def agent_output(n: Node) -> UrgenciaEstimada:
    (event,) = [e for e in urgency_events(n) if e.payload["source"] == "agente"]
    return UrgenciaEstimada.model_validate(event.payload)


def case_for(text: str = GATO, *, setup=None) -> TriageCase:
    """Un caso real del nodo (N7 con clasificador en la zona gris), sin agente."""
    n = node()
    if setup:
        setup(n)
    n.receive_message(message(text))
    (event,) = [e for e in urgency_events(n) if e.payload["report_id"] == "a1b2c3d4-7"]
    case = AgenteTriaje.prepare(event, n.store)
    assert case is not None
    return case


def run(replies, case: TriageCase | None = None, **kwargs) -> UrgenciaEstimada:
    return AgenteTriaje(ScriptedLLM(replies), **kwargs).run(case or case_for())


def final(level: int, quote: str | None = None, **extra) -> LLMReply:
    answer = {"level": level, "reason": "lo dice el texto", **extra}
    if quote is not None:
        answer["quote"] = quote
    return LLMReply(final=answer)


def tool(name: str, **args) -> LLMReply:
    return LLMReply(tool_name=name, tool_args=args)


# --- Guardias (regla 1) -------------------------------------------------------------------


def test_decide_level_nunca_baja_el_piso_ni_mas_de_un_nivel() -> None:
    """Todas las combinaciones: lo aceptado respeta la regla 1."""
    for floor, current, proposed, quote in itertools.product(
        Urgency, Urgency, range(0, 6), [None, "gato", "perro"]
    ):
        if current < floor:
            continue
        level, why = decide_level(floor, current, proposed, quote, GATO)
        if level is None:
            assert why
            continue
        assert level >= floor
        assert level >= current - 1
        if level < current:
            assert quote == "gato" and "bajada por el agente" in why


def test_decide_level_casos() -> None:
    alta, media = Urgency.ALTA, Urgency.MEDIA
    assert decide_level(Urgency.BAJA, alta, 4, None, GATO) == (
        Urgency.CRITICA,
        "sube alta → crítica",
    )
    assert decide_level(Urgency.BAJA, alta, 3, None, GATO) == (alta, "confirma alta")
    assert decide_level(Urgency.BAJA, alta, 2, "un GATO herido", GATO)[0] == media
    assert decide_level(Urgency.BAJA, alta, 1, "gato", GATO)[0] is None  # dos niveles
    assert decide_level(alta, alta, 2, "gato", GATO)[0] is None  # bajo el piso
    assert decide_level(Urgency.BAJA, alta, 2, None, GATO)[0] is None  # sin cita
    assert decide_level(Urgency.BAJA, alta, 2, "un perro", GATO)[0] is None  # cita inventada
    for bad in ("3", 3.5, True, None, 0, 5):
        assert decide_level(Urgency.BAJA, alta, bad, None, GATO)[0] is None


def test_la_cita_ignora_tildes_mayusculas_y_espacios() -> None:
    assert quote_in_text("Gato  HERÍDO", "un gato herido")
    assert not quote_in_text("ga", "un gato")  # muy corta
    assert not quote_in_text("gato", None)


def test_solo_actua_en_la_zona_gris_y_nunca_sobre_si_mismo() -> None:
    base = dict(report_id="a1b2c3d4-7", level=3, floor=1)
    assert needs_agent(UrgenciaEstimada(**base, source="clasificador", ambiguous=True))
    assert not needs_agent(UrgenciaEstimada(**base, source="clasificador"))
    assert not needs_agent(UrgenciaEstimada(**base, source="agente", ambiguous=True))


# --- Criterio de "Terminado cuando" -------------------------------------------------------


def test_con_null_llm_queda_ambiguo_revisar() -> None:
    out = AgenteTriaje(NullLLM()).run(case_for())
    assert out.source == "agente" and out.ambiguous
    assert out.level == Urgency.ALTA  # el de N7: sin LLM no se baja nada
    assert out.steps[-1].startswith("ambiguo, revisar: LLM no disponible")


def test_sin_ningun_llm_de_la_cadena_queda_ambiguo_revisar() -> None:
    out = AgenteTriaje(FallbackLLM([NullLLM(), NullLLM()])).run(case_for())
    assert out.ambiguous and out.level == Urgency.ALTA


def test_nunca_baja_el_piso() -> None:
    case = case_for()
    alto = case.urgency.model_copy(update={"floor": Urgency.ALTA})  # piso alto y ambiguo
    out = run([final(2, quote="gato herido")], dataclasses.replace(case, urgency=alto))
    assert out.level == Urgency.ALTA and out.ambiguous
    assert "bajo el piso" in out.steps[-1]


def test_maximo_seis_pasos() -> None:
    out = run([tool("incidentes_cercanos")] * 20)
    assert out.ambiguous and out.level == Urgency.ALTA
    assert sum(s.startswith("paso ") for s in out.steps) == MAX_STEPS
    assert f"se agotaron los {MAX_STEPS} pasos" in out.steps[-1]


# --- Decisiones del agente ----------------------------------------------------------------


def test_baja_un_nivel_con_cita() -> None:
    out = run([tool("incidentes_cercanos", minutos=30), final(2, quote="gato herido")])
    assert (out.level, out.ambiguous, out.floor) == (Urgency.MEDIA, False, Urgency.BAJA)
    assert out.steps[-1] == (
        "decisión: bajada por el agente: alta → media; motivo: lo dice el texto; "
        "cita: «gato herido»"
    )
    assert out.probabilities == GRAY  # conserva lo de N7


def test_sube_sin_limite_con_fijar_urgencia() -> None:
    out = run([tool("fijar_urgencia", level=4, reason="hay niños", verify_now=True)])
    assert (out.level, out.ambiguous, out.verify_now) == (Urgency.CRITICA, False, True)
    assert out.steps[-1].startswith("decisión: sube alta → crítica")


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        (final(1, quote="gato"), "más de un nivel"),
        (final(2), "sin una cita textual"),
        (final(2, quote="un perro"), "sin una cita textual"),
        (LLMReply(final={"level": 2, "quote": "gato"}), "sin motivo"),
        (final(3, verify_now="si"), "verify_now"),
        (final("alta"), "nivel propuesto inválido"),
        (tool("borrar_reporte"), "herramienta no permitida"),
        (tool("incidentes_cercanos", minutos=999), "argumentos inválidos"),
        (tool("incidentes_cercanos", radio=3), "argumentos inválidos"),
        (tool("marcar_verificar_ya"), "argumentos inválidos"),
    ],
)
def test_lo_invalido_va_a_revision_con_el_nivel_de_n7(reply: LLMReply, why: str) -> None:
    out = run([reply])
    assert out.ambiguous and out.level == Urgency.ALTA
    assert out.steps[-1].startswith("ambiguo, revisar") and why in out.steps[-1]


def test_marcar_verificar_ya_se_conserva_aunque_vaya_a_revision() -> None:
    out = run([tool("marcar_verificar_ya", motivo="se oyen gritos")])  # luego se acaba el guion
    assert out.verify_now and out.ambiguous
    assert any("verificar ya: se oyen gritos" in s for s in out.steps)


def test_un_proveedor_que_lanza_cualquier_cosa_no_tumba_nada() -> None:
    class Broken:
        name = "roto"

        def complete(self, messages, tools):
            raise RuntimeError("bug")

    out = AgenteTriaje(Broken()).run(case_for())
    assert out.ambiguous and "el LLM falló (RuntimeError)" in out.steps[-1]


def test_si_se_rompe_el_grafo_queda_ambiguo(monkeypatch) -> None:
    agent = AgenteTriaje(ScriptedLLM([]))
    monkeypatch.setattr(agent, "_graph", None)
    out = agent.run(case_for())
    assert out.ambiguous and "falla interna" in out.steps[-1]


def test_tiempo_agotado() -> None:
    ticks = iter(range(0, 1000, 100))
    out = run([tool("incidentes_cercanos")] * 5, clock=lambda: next(ticks), time_budget_s=150)
    assert out.ambiguous and "se agotó el tiempo" in out.steps[-1]


def test_el_llm_recibe_el_caso_y_el_resultado_de_las_herramientas() -> None:
    seen = []

    class Spy(ScriptedLLM):
        def complete(self, messages, tools):
            seen.append((list(messages), [t.name for t in tools]))
            return super().complete(messages, tools)

    AgenteTriaje(Spy([tool("incidentes_cercanos"), final(3)])).run(case_for())
    first, names = seen[0]
    assert names == ["incidentes_cercanos", "marcar_verificar_ya", "fijar_urgencia"]
    assert GATO in first[1].content and '"piso_de_las_reglas": "baja"' in first[1].content
    second, _ = seen[1]
    assert [m.role for m in second] == ["system", "user", "assistant", "tool"]


# --- Herramienta incidentes_cercanos ------------------------------------------------------


def test_incidentes_cercanos_mismo_barrio_sin_el_propio() -> None:
    def setup(n: Node) -> None:
        n.receive_message(message("colapso casa 3 atrapados", seq=1, flags=Flag.ATRAPADOS))
        n.receive_message(message("otro herido lejos", seq=2, barrio=5))

    found = incidentes_cercanos(case_for(setup=setup), 60)
    assert [(f["nivel"], f["reportes"]) for f in found] == [("crítica", 1)]
    with pytest.raises(ValueError):
        incidentes_cercanos(case_for(), 5)


# --- En el nodo ---------------------------------------------------------------------------


def test_el_nodo_publica_la_urgencia_del_agente_y_la_tarjeta_la_usa() -> None:
    a = node(ScriptedLLM([final(2, quote="gato herido")]))
    a.receive_message(message(GATO))
    n7, agent = urgency_events(a)
    assert agent.payload["source"] == "agente" and agent.refs == (n7.id,)
    assert agent.category == "derivado" and agent.received_at == NOW
    (card,) = incident_cards(a.store.since({}))
    assert card.level == Urgency.MEDIA  # vale la más reciente de la cadena
    detail = incident_detail(a.store.since({}), card.incident_id)
    texts = [s.detail for s in detail.steps]
    assert "clasificador: nivel alta; ambiguo, revisar" in texts
    assert any("bajada por el agente: alta → media" in t for t in texts)


def test_con_null_llm_la_tarjeta_dice_ambiguo_revisar() -> None:
    a = node(NullLLM())
    a.receive_message(message(GATO))
    assert agent_output(a).ambiguous
    (card,) = incident_cards(a.store.since({}))
    assert card.level == Urgency.ALTA
    detail = incident_detail(a.store.since({}), card.incident_id)
    assert "agente: nivel alta; ambiguo, revisar" in [s.detail for s in detail.steps]


def test_sin_llm_no_hay_agente() -> None:
    a = node()
    a.receive_message(message(GATO))
    assert [e.payload["source"] for e in urgency_events(a)] == ["clasificador"]


def test_fuera_de_la_zona_gris_no_llama_al_llm() -> None:
    a = node(ScriptedLLM([]))  # si lo llamara, publicaría un "ambiguo, revisar"
    a.receive_message(message("3 atrapados", flags=Flag.ATRAPADOS))
    assert [e.payload["source"] for e in urgency_events(a)] == ["reglas"]


def test_si_falla_el_agente_el_nodo_sigue(monkeypatch) -> None:
    a = node(ScriptedLLM([]))

    def boom(case):
        raise RuntimeError("bug")

    monkeypatch.setattr(a.triaje, "run", boom)
    assert not a.receive_message(message(GATO)).duplicate
    assert "incidente_agrupado" in {e.type for e in a.store.since({})}


def test_solo_el_nodo_dueno_corre_el_agente() -> None:
    nodes = cluster()
    for n in nodes.values():
        n.classifier, n.triaje = GrayClassifier(), AgenteTriaje(ScriptedLLM([final(3)]))
    nodes["A"].receive_message(message(GATO))
    asyncio.run(sync(nodes))
    for name in "BC":
        events = nodes[name].store.since({})
        assert not [e for e in events if e.origin_node == name]
        sources = [e.payload["source"] for e in events if e.type == "urgencia_estimada"]
        assert sources == ["clasificador", "agente"]


def test_con_bucle_el_agente_corre_aparte_y_no_frena_el_ack() -> None:
    release = threading.Event()

    class Slow(ScriptedLLM):
        def complete(self, messages, tools):
            assert release.wait(5)
            return super().complete(messages, tools)

    async def scenario() -> None:
        a = node(Slow([final(3)]))
        ack = a.receive_message(message(GATO))  # vuelve sin esperar al LLM
        assert ack.status == "recibido"
        assert [e.payload["source"] for e in urgency_events(a)] == ["clasificador"]
        assert "incidente_agrupado" in {e.type for e in a.store.since({})}
        release.set()
        await asyncio.gather(*a.agent_tasks)
        await asyncio.sleep(0)  # deja correr el callback que publica
        assert agent_output(a).level == Urgency.ALTA and not a.agent_tasks

    asyncio.run(scenario())


def test_llm_unavailable_es_la_unica_salida_sin_respuesta() -> None:
    with pytest.raises(LLMUnavailable):
        NullLLM().complete([], [])


# --- Evaluación ---------------------------------------------------------------------------


def test_evaluacion_cuenta_lo_que_resuelve_el_agente() -> None:
    from guadua.agents.triaje_eval import case_of, run_agent
    from guadua.classifier.synthetic import Row

    row = Row(GATO, Incident.HERIDO, Urgency.MEDIA, "ambiguo", "")
    urgency = UrgenciaEstimada(
        report_id="00000000-1", level=3, floor=1, source="clasificador", ambiguous=True
    )
    llm = ScriptedLLM([final(2, quote="gato herido"), tool("borrar")])
    result = run_agent([(row, case_of(row, 0, urgency))] * 2, llm)
    assert result["resueltos"] == result["bien"] == result["bajadas"] == 1
    assert result["a_revision"] == result["invalidas"] == 1
    assert result["pasos_promedio"] == 1.0
