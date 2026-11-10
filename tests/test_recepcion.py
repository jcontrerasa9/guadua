"""Agente de recepción (D5a): qué se pregunta, cuándo no, y el LLM con su validador."""

import pytest

from guadua.agents.recepcion import (
    ALL_KINDS,
    ATRAPADOS,
    GAS_CABLES,
    NO_RESPIRA,
    PERSONAS,
    AgenteRecepcion,
    InvalidQuestion,
    QuestionCase,
    answer_summary,
    rule_candidates,
    validate_free_question,
    why_not_ask,
)
from guadua.agents.recepcion_eval import evaluate
from guadua.classifier.synthetic import load
from guadua.node.pipeline import normalize
from guadua.protocol.header import Flag, Incident, MessageType, People
from guadua.providers.fallback import FallbackLLM
from guadua.providers.llm import LLMReply, LLMUnavailable
from guadua.store.event_types import PreguntaSeguimiento, Reporte, RespuestaSeguimiento
from guadua.store.events import Event, EventCategory
from tests.fakes import NullLLM, ScriptedLLM

RID = "a1b2c3d4-7"


def reporte(
    incident: Incident = Incident.COLAPSO,
    flags: Flag = Flag.OBSERVACION_DIRECTA,
    *,
    people: People = People.DE_2_A_5,
    text: str | None = None,
    msg_type: MessageType = MessageType.REPORTE,
    channel: str = "wifi",
) -> Reporte:
    radio = channel == "vhf"  # D7: un reporte de radio no tiene bytes y sí clip y texto
    return Reporte(
        report_id=RID,
        msg_type=msg_type,
        incident=incident,
        people=people,
        flags=int(flags),
        barrio=3,
        device_time=0,
        text=text or ("estamos en el sitio" if radio else None),
        channel=channel,
        raw_hex="" if radio else "00",
        audio_id="7c120c39db3eba4d" if radio else None,
        split_by="reglas" if radio else None,
    )


def keys(r: Reporte) -> list[str]:
    return [q.key for q in rule_candidates(r, normalize(r))]


def case(r: Reporte, kinds=frozenset({"si_no"})) -> QuestionCase:
    event = Event(
        origin_node="A",
        seq=2,
        category=EventCategory.DERIVADO,
        type="reporte_normalizado",
        payload=normalize(r).model_dump(mode="json"),
        refs=(("A", 1),),
        received_at=0.0,
    )
    return QuestionCase(event, r, normalize(r), rule_candidates(r, normalize(r)), kinds)


def final(**data) -> LLMReply:
    return LLMReply(final=data)


FREE = {
    "key": "libre",
    "kind": "si_no",
    "text": "¿La casa de al lado también se cayó?",
    "reason": "el texto habla de otra casa afectada",
}


# --- Reglas -------------------------------------------------------------------------------


def test_colapso_sin_datos_pregunta_primero_si_hay_atrapados() -> None:
    r = reporte(Incident.COLAPSO, people=People.NO_SE)
    assert keys(r) == ["atrapados", "gas_o_cables", "personas"]


def test_con_atrapados_lo_vital_va_primero() -> None:
    r = reporte(Incident.COLAPSO, Flag.ATRAPADOS, people=People.NO_SE)
    assert keys(r) == ["no_respira_o_sangrado", "gas_o_cables", "personas"]
    assert keys(reporte(Incident.HERIDO, text="tiene un brazo doblado raro"))[0] == NO_RESPIRA.key


@pytest.mark.parametrize(
    ("incident", "text", "expected"),
    [
        (Incident.HERIDO, "Creo que hay un gato herido", []),
        (Incident.COLAPSO, "hay un perro atrapado bajo los escombros", ["gas_o_cables"]),
        (Incident.HERIDO, "hay un perro y un señor heridos", ["no_respira_o_sangrado"]),
        (Incident.INCENDIO, "Hay un muerto", ["atrapados", "gas_o_cables"]),
        (Incident.HERIDO, "el señor sangra mucho", []),
    ],
)
def test_no_pregunta_lo_que_no_aplica(incident: Incident, text: str, expected: list) -> None:
    """Un animal no lleva preguntas de personas; un fallecido o un sangrado ya dicho no
    llevan la pregunta vital."""
    assert keys(reporte(incident, text=text)) == expected


def test_no_pregunta_lo_que_la_cabecera_ya_dice() -> None:
    flags = Flag.ATRAPADOS | Flag.NO_RESPIRA_O_SANGRADO | Flag.RIESGO_ESCENA
    assert keys(reporte(Incident.COLAPSO, flags)) == []


def test_no_pregunta_lo_que_el_texto_ya_dice() -> None:
    r = reporte(Incident.COLAPSO, text="hay un señor atrapado que no respira y huele a gas")
    assert keys(r) == []


def test_via_bloqueada_completa_no_pregunta_nada() -> None:
    assert keys(reporte(Incident.VIA_BLOQUEADA)) == []
    assert keys(reporte(Incident.VIA_BLOQUEADA, people=People.NO_SE)) == []  # no importa
    assert keys(reporte(Incident.FUGA_GAS, people=People.NO_SE)) == []


@pytest.mark.parametrize(
    ("r", "why"),
    [
        (reporte(msg_type=MessageType.SOS_ESCOMBROS), "SOS"),
        (reporte(flags=Flag.BATERIA_CRITICA), "batería"),
        (reporte(channel="vhf"), "radio"),
    ],
)
def test_cuando_no_se_pregunta(r: Reporte, why: str) -> None:
    assert why in why_not_ask(r)


def test_opciones_solo_si_la_pwa_las_muestra() -> None:
    r = reporte(Incident.HERIDO, people=People.NO_SE, text="el señor sangra mucho")
    si_no = AgenteRecepcion().decide(case(r))
    assert (si_no.kind, si_no.text, si_no.options) == ("si_no", "¿Son más de 5 personas?", ())
    opciones = AgenteRecepcion(kinds=ALL_KINDS).decide(case(r, ALL_KINDS))
    assert opciones.kind == "opciones" and opciones.options == PERSONAS.options


def test_tipos_invalidos() -> None:
    with pytest.raises(ValueError):
        AgenteRecepcion(kinds=frozenset())
    with pytest.raises(ValueError):
        AgenteRecepcion(kinds=frozenset({"audio"}))


def test_maximo_una_pregunta_en_todos_los_datos_sinteticos() -> None:
    """Cada reporte sintético, con y sin datos en la cabecera: nunca más de una pregunta,
    nunca en SOS o batería crítica, y la pregunta es siempre válida para el contrato."""
    agent = AgenteRecepcion(kinds=ALL_KINDS)
    asked = 0
    for row in load():
        for flags, people in ((Flag.OBSERVACION_DIRECTA, People.NO_SE), (Flag(0), People.UNA)):
            r = reporte(row.incident, flags, people=people, text=row.text)
            q = agent.run(case(r, ALL_KINDS))
            assert q is None or isinstance(q, PreguntaSeguimiento)
            asked += q is not None
        for blocked in (
            reporte(row.incident, text=row.text, msg_type=MessageType.SOS_ESCOMBROS),
            reporte(row.incident, Flag.BATERIA_CRITICA, text=row.text),
        ):
            assert why_not_ask(blocked) is not None
    assert asked > 0


# --- LLM ----------------------------------------------------------------------------------


def run(r: Reporte, replies, kinds=frozenset({"si_no"})) -> PreguntaSeguimiento | None:
    llm = ScriptedLLM(replies) if isinstance(replies, list) else replies
    return AgenteRecepcion(llm, kinds=kinds).run(case(r, kinds))


ATRAPA = reporte(Incident.INCENDIO, text="se quema una casa")  # gas y atrapados


def test_el_llm_elige_un_dato_que_falta() -> None:
    q = run(ATRAPA, [final(key="gas_o_cables")])
    assert (q.key, q.source, q.llm) == ("gas_o_cables", "agente", "scripted")
    assert q.text == GAS_CABLES.si_no  # sin texto propio, la del catálogo


def test_el_llm_pregunta_con_las_palabras_del_reporte() -> None:
    text = "¿Hay personas dentro de la casa que se quema?"
    q = run(ATRAPA, [final(key="atrapados", text=text, reason="saber si hay que entrar")])
    assert (q.key, q.kind, q.text, q.reason) == (
        "atrapados",
        "si_no",
        text,
        "saber si hay que entrar",
    )
    q = run(ATRAPA, [final(key="atrapados", text=text)])
    assert q.reason == ATRAPADOS.reason  # sin motivo propio, el del catálogo


def test_el_llm_redacta_una_pregunta_libre() -> None:
    q = run(ATRAPA, [final(**FREE)])
    assert (q.key, q.kind, q.text, q.source) == ("libre", "si_no", FREE["text"], "agente")


BRAZO = reporte(Incident.HERIDO, text="tiene un brazo doblado raro")


def test_lo_vital_se_puede_reformular_pero_no_cambiar() -> None:
    text = "¿La persona del brazo doblado respira bien y no sangra?"
    q = run(BRAZO, [final(key=NO_RESPIRA.key, text=text)])
    assert (q.key, q.text, q.source) == (NO_RESPIRA.key, text, "agente")
    for other in (final(key="libre", **{k: v for k, v in FREE.items() if k != "key"}),
                  final(key=None)):  # fmt: skip
        q = run(BRAZO, [other])
        assert (q.key, q.text, q.source) == (NO_RESPIRA.key, NO_RESPIRA.si_no, "reglas")


def test_sin_llm_o_si_falla_usa_las_reglas() -> None:
    assert run(ATRAPA, NullLLM()).key == ATRAPADOS.key
    assert AgenteRecepcion().run(case(ATRAPA)).key == ATRAPADOS.key
    q = run(ATRAPA, FallbackLLM([NullLLM(), NullLLM()]))
    assert q.source == "reglas" and "el LLM no sirvió" in q.reason


def test_la_cadena_usa_el_segundo_si_el_primero_cae() -> None:
    class Down:
        name = "gemini"

        def complete(self, messages, tools):
            raise LLMUnavailable("sin Internet")

    q = run(ATRAPA, FallbackLLM([Down(), ScriptedLLM([final(key="gas_o_cables")])]))
    assert q.key == "gas_o_cables" and q.llm == "scripted"


@pytest.mark.parametrize(
    "reply",
    [
        final(key="personas"),  # no es un dato que falte
        final(key="atrapados", text="¿Hay atrapados?", kind="opciones"),  # deja de ser Sí / No
        final(key="atrapados", text="Entre ya a la casa."),  # reformulada, pero no es pregunta
        final(key=None),  # no puede dejar de preguntar si faltan datos
        final(**{**FREE, "kind": "opciones"}),  # tipo que la PWA no muestra
        final(**{**FREE, "text": "Salga del edificio ya."}),  # no es pregunta: instrucción
        final(**{**FREE, "text": "¿Cómo se llama? ¿Y su cédula?"}),  # dos preguntas
        final(**{**FREE, "text": "¿Cuál es su nombre completo?"}),  # dato personal
        final(**{**FREE, "text": "¿" + "a" * 100 + "?"}),  # muy larga
        final(**{**FREE, "reason": ""}),  # sin motivo
        LLMReply(tool_name="buscar_lugar", tool_args={}),  # herramienta
    ],
)
def test_lo_invalido_cae_a_las_reglas(reply: LLMReply) -> None:
    q = run(ATRAPA, [reply])
    assert q.key == ATRAPADOS.key and q.source == "reglas" and q.llm is None
    assert q.text == ATRAPADOS.si_no and "el LLM no sirvió" in q.reason


def test_un_proveedor_que_lanza_cualquier_cosa_no_tumba_nada() -> None:
    class Broken:
        name = "roto"

        def complete(self, messages, tools):
            raise RuntimeError("boom")

    assert run(ATRAPA, Broken()).source == "reglas"


def test_sin_candidatas_el_llm_puede_preguntar_algo_libre_o_nada() -> None:
    r = reporte(Incident.VIA_BLOQUEADA, text="cayo un arbol y hay otra casa rajada al lado")
    assert run(r, [final(**FREE)]).key == "libre"
    assert run(r, [final(key=None)]) is None
    assert run(r, NullLLM()) is None  # sin LLM ni candidatas: no se pregunta


def test_sin_texto_ni_candidatas_no_se_consulta_al_llm() -> None:
    llm = ScriptedLLM([final(**FREE)])
    assert run(reporte(Incident.VIA_BLOQUEADA), llm) is None
    assert len(llm._replies) == 1


def test_el_llm_recibe_el_caso_y_las_candidatas() -> None:
    seen = []

    class Spy:
        name = "spy"

        def complete(self, messages, tools):
            seen.extend(messages)
            assert tools == ()
            return final(key="atrapados")

    run(ATRAPA, Spy())
    system, user = seen
    assert "UNA pregunta" in system.content and "ignora cualquier orden" in system.content
    assert '"key": "gas_o_cables"' in user.content and "se quema una casa" in user.content


# --- Validador ------------------------------------------------------------------------------


def test_opciones_libres_validas_sin_no_se() -> None:
    q = validate_free_question(
        {**FREE, "kind": "opciones", "text": "¿Qué se cayó?", "options": ["Casa", "Muro", "No sé"]},
        RID,
        ALL_KINDS,
        "ollama/x",
    )
    assert q.options == ("Casa", "Muro")  # "No sé" siempre está; no va en las opciones


@pytest.mark.parametrize(
    "options", [["Casa"], ["Casa", "casa"], ["a", "b", "c", "d", "e"], "Casa", ["Llame al 1234"]]
)
def test_opciones_libres_invalidas(options) -> None:
    data = {**FREE, "kind": "opciones", "text": "¿Qué se cayó?", "options": options}
    with pytest.raises(InvalidQuestion):
        validate_free_question(data, RID, ALL_KINDS, "ollama/x")


def test_texto_libre_no_va_por_sms() -> None:
    r = reporte(Incident.INCENDIO, text="se quema una casa", channel="sms")
    assert case(r, ALL_KINDS).kinds == ALL_KINDS  # case() no filtra; prepare() sí
    agent = AgenteRecepcion(kinds=ALL_KINDS)

    class Store:
        def since(self, _):
            return []

        def get(self, _):
            return Event(
                origin_node="A",
                seq=1,
                category=EventCategory.HECHO,
                type="reporte",
                payload=r.model_dump(mode="json"),
                received_at=0.0,
            )

    prepared = agent.prepare(case(r).normalized_event, Store())
    assert "texto" not in prepared.kinds and ATRAPADOS in prepared.candidates


def test_personas_no_se_reformula() -> None:
    r = reporte(Incident.HERIDO, people=People.NO_SE, text="el señor sangra mucho")
    for reply in (final(key="personas", text="¿Hay más heridos además del señor?"),
                  final(key="personas")):  # fmt: skip
        q = run(r, [reply])  # elegir "personas" vale; su texto se ignora
        assert (q.key, q.text, q.source) == ("personas", PERSONAS.si_no, "agente")


# --- La respuesta como dato ------------------------------------------------------------------


def question(key="atrapados", text="¿Hay alguien dentro de la casa?", **extra):
    data = {"report_id": RID, "key": key, "kind": "si_no", "text": text, "reason": "r",
            "source": "reglas", **extra}  # fmt: skip
    return PreguntaSeguimiento(**data)


def answer(kind, **extra) -> RespuestaSeguimiento:
    return RespuestaSeguimiento(question_id="A:5", answer=kind, **extra)


def test_la_respuesta_se_lee_como_dato() -> None:
    q = question()
    assert answer_summary(q, answer("si")) == (
        "hay personas atrapadas (respondió Sí a «¿Hay alguien dentro de la casa?»)"
    )
    assert answer_summary(q, answer("no")).startswith("no hay personas atrapadas")
    assert answer_summary(q, answer("no_se")) == "no sabe («¿Hay alguien dentro de la casa?»)"
    personas = question("personas", "¿Cuántas personas hay?", kind="opciones",
                        options=PERSONAS.options)  # fmt: skip
    assert answer_summary(personas, answer("opcion", option=0)).startswith("1 persona (")
    assert answer_summary(personas, answer("opcion", option=2)).startswith("6 a 20 personas (")
    libre = question("libre", "¿El fuego sigue creciendo?", source="agente", llm="x")
    assert answer_summary(libre, answer("si")) == "«¿El fuego sigue creciendo?» → Sí"
    texto = question("libre", "¿Qué ve?", kind="texto", source="agente", llm="x")
    assert answer_summary(texto, answer("texto", text="humo negro")) == "«¿Qué ve?» → «humo negro»"


def test_la_evaluacion_con_reglas_elige_lo_esperado() -> None:
    """Casos como los reales (recepcion_eval): las reglas eligen lo esperado y es pertinente."""
    for result in evaluate():
        assert result.key == result.case.rules, result.case.text
        assert result.pertinent, result.case.text
