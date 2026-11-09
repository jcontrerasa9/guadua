"""Resiliencia (N12): latidos, caída de un nodo, reasignación del dueño y recuperación.

Tres nodos A ↔ B ↔ C sobre la red simulada, con SQLite en disco (el nodo que vuelve la
reabre) y un reloj simulado para los latidos. "Caer" es desconectar el nodo y dejar de
correrlo; "volver" es crear otro `Node` con el mismo archivo.
"""

import asyncio

import pytest

from guadua.channel.profiles import IDEAL, LORA
from guadua.channel.simulated import SimulatedNetwork
from guadua.node.api import DecisionRequest
from guadua.node.config import NodeConfig
from guadua.node.intake import parse_message, reporte_event
from guadua.node.pipeline import normalized_event
from guadua.node.service import Node
from guadua.node.views import incident_cards, incident_detail
from guadua.services import resilience
from guadua.services.failover import (
    SimClock,
    converged,
    measure_failover,
    pending_of,
    run_round,
)
from guadua.services.resilience import Ownership, pending_work
from guadua.store.event_types import validate_event
from guadua.store.events import Event, EventCategory
from guadua.store.sqlite import SQLiteEventStore
from tests.fakes import ScriptedLLM
from tests.test_node_cluster import REPORT
from tests.test_triaje import GATO, GrayClassifier, final, message

NOW = 1_794_573_600.0
PEERS = {"A": ("B",), "B": ("A", "C"), "C": ("B",)}


class Cluster:
    def __init__(self, tmp_path, profile=IDEAL, seed=0, **node_kwargs) -> None:
        self.tmp_path = tmp_path
        self.clock = SimClock()
        self.network = SimulatedNetwork(seed=seed, time_scale=0)
        self.network.link("A", "B", profile)
        self.network.link("B", "C", profile)
        self.node_kwargs = node_kwargs
        self.nodes = {n: self.start(n) for n in "ABC"}

    def start(self, name: str) -> Node:
        """Arranca (o vuelve a arrancar) un nodo con su SQLite de siempre."""
        return Node(
            name,
            SQLiteEventStore(self.tmp_path / f"{name}.db"),
            transport=self.network.endpoint(name),
            peers=PEERS[name],
            clock=lambda: NOW,
            monotonic=self.clock,
            **{"classifier": None, **self.node_kwargs},
        )

    def __getitem__(self, name: str) -> Node:
        return self.nodes[name]

    def alive(self, *names: str) -> list[Node]:
        return [self.nodes[n] for n in names]

    async def rounds(self, *names: str, count: int = 1) -> None:
        for _ in range(count):
            await run_round(self.alive(*(names or "ABC")), self.clock)

    async def spread(self, count: int = 3) -> None:
        """Solo gossip, sin rondas de resiliencia: lo que A guardó llega a B y C antes de que
        A alcance a procesarlo (A se cae justo después de enviarlo)."""
        nodes = self.alive("A", "B", "C")
        for _ in range(count):
            for node in nodes:
                await node.gossip.tick()
            while sum([await node.gossip.drain() for node in nodes]):
                pass

    def kill(self, name: str) -> None:
        for peer in PEERS[name]:
            self.network.disconnect(name, peer)

    def revive(self, name: str) -> Node:
        for peer in PEERS[name]:
            self.network.connect(name, peer)
        self.nodes[name] = self.start(name)
        return self.nodes[name]


def received_only(node: Node, data: bytes = REPORT) -> Event:
    """El nodo guardó el reporte y se cayó antes de derivar nada."""
    event = reporte_event(parse_message(data), node_id=node.node_id, seq=node.next_seq(),
                          received_at=NOW)  # fmt: skip
    node.store.append(event)
    return event


def derived_by(node: Node, report_id: str = "a1b2c3d4-7") -> dict[str, set[str]]:
    """Tipo de derivado → nodos que lo publicaron para ese reporte (o su incidente)."""
    out: dict[str, set[str]] = {}
    for e in node.store.since({}):
        p = e.payload
        mine = report_id in (p.get("report_id"), p.get("incident_id"), *p.get("report_ids", ()))
        if mine and e.category == EventCategory.DERIVADO:
            out.setdefault(e.type, set()).add(e.origin_node)
    return out


def takeovers(node: Node) -> list[Event]:
    return [e for e in node.store.since({}) if e.type == "reasignacion"]


# --- Dueño y pendientes (sin red) ----------------------------------------------------------


def reasignacion(by: str, seq: int, from_node: str, up_to: int) -> Event:
    event = Event(
        origin_node=by, seq=seq, category=EventCategory.HECHO, type="reasignacion",
        payload={"from_node": from_node, "up_to_seq": up_to, "silent_s": 12.0},
        refs=((from_node, up_to),), received_at=NOW,
    )  # fmt: skip
    validate_event(event)
    return event


def test_sin_reasignaciones_el_dueno_es_el_origen() -> None:
    assert Ownership().owner_of(("A", 3)) == "A"


def test_la_reasignacion_cubre_hasta_up_to_seq() -> None:
    own = Ownership([reasignacion("B", 9, "A", 5)])
    assert own.owner_of(("A", 5)) == "B"
    assert own.owner_of(("A", 6)) == "A"  # lo que A hizo después (aislado o al volver) es suyo


def test_si_dos_toman_lo_mismo_gana_el_id_mas_bajo() -> None:
    """Cae B: A y C lo toman cada uno por su lado (están partidos). Al juntarse, todos ven A."""
    for order in ([("C", "B"), ("A", "B")], [("A", "B"), ("C", "B")]):
        own = Ownership(reasignacion(by, 4, f, 7) for by, f in order)
        assert own.owner_of(("B", 3)) == "A"


def test_la_reasignacion_se_encadena() -> None:
    """B tomó lo de A con B:9; luego cae B y C toma lo de B hasta B:9: lo de A pasa a C."""
    own = Ownership([reasignacion("B", 9, "A", 5), reasignacion("C", 4, "B", 9)])
    assert own.owner_of(("A", 2)) == "C"
    assert (
        Ownership([reasignacion("B", 9, "A", 5), reasignacion("C", 4, "B", 8)]).owner_of(("A", 2))
        == "B"
    )  # C no sabía de la toma de B


def test_pendientes_por_etapa(tmp_path) -> None:
    a = Node("A", SQLiteEventStore(), clock=lambda: NOW, classifier=None)
    reporte = received_only(a)
    assert [w.kind for w in pending_work(a.store.since({}))] == ["normalizar"]
    normalized = normalized_event(reporte, node_id="A", seq=a.next_seq())
    a.store.append(normalized)
    assert [w.kind for w in pending_work(a.store.since({}))] == ["urgencia", "agrupar"]
    a.do_work(pending_work(a.store.since({}))[0])  # urgencia
    a.do_work(pending_work(a.store.since({}))[0])  # agrupar → la ficha sale sola
    assert pending_work(a.store.since({})) == []


def test_un_nodo_sano_no_tiene_pendientes() -> None:
    a = Node("A", SQLiteEventStore(), clock=lambda: NOW, classifier=None)
    a.receive_message(REPORT)
    assert pending_work(a.store.since({})) == []
    assert a.resilience.check() == []


# --- Caída de A: B toma sus pendientes ----------------------------------------------------


def test_al_caer_a_b_toma_sus_pendientes_y_todos_lo_ven(tmp_path) -> None:
    async def scenario() -> None:
        cl = Cluster(tmp_path)
        received_only(cl["A"])
        await cl.spread()  # el reporte llega a B y C; nadie más que A deriva
        assert derived_by(cl["C"]) == {}
        cl.kill("A")
        await cl.rounds("B", "C", count=4)  # 8 s: aún no se da por caído
        assert takeovers(cl["B"]) == []
        await cl.rounds("B", "C", count=3)  # 14 s sin latidos de A
        (event,) = takeovers(cl["C"])
        assert event.origin_node == "B" and event.payload["from_node"] == "A"
        assert event.payload["silent_s"] > cl["B"].resilience.timeout
        assert derived_by(cl["C"]) == {
            "reporte_normalizado": {"B"}, "urgencia_estimada": {"B"},
            "incidente_agrupado": {"B"}, "ficha": {"B"},
        }  # fmt: skip
        assert cl["C"].ownership.owner_of(("A", 1)) == "B"  # C llega a la misma conclusión
        assert not cl["C"].owns(("A", 1)) and takeovers(cl["B"]) == [event]
        (card,) = incident_cards(cl["C"].store.since({}))
        assert card.status == "pendiente" and card.ficha_id.startswith("B:")
        (t,) = cl["B"].resilience.stats.takeovers
        assert t.from_node == "A" and t.steps >= 1

    asyncio.run(scenario())


def test_b_termina_lo_que_a_dejo_a_medias(tmp_path) -> None:
    async def scenario() -> None:
        cl = Cluster(tmp_path)
        reporte = received_only(cl["A"])
        cl["A"].store.append(normalized_event(reporte, node_id="A", seq=cl["A"].next_seq()))
        await cl.spread()
        cl.kill("A")
        await cl.rounds("B", "C", count=7)
        found = derived_by(cl["C"])
        assert found["reporte_normalizado"] == {"A"}  # lo de A no se repite
        assert found["urgencia_estimada"] == {"B"} and found["ficha"] == {"B"}

    asyncio.run(scenario())


def test_sin_pendientes_no_hay_reasignacion(tmp_path) -> None:
    async def scenario() -> None:
        cl = Cluster(tmp_path)
        cl["A"].receive_message(REPORT)  # A alcanzó a hacerlo todo
        await cl.rounds(count=3)
        cl.kill("A")
        await cl.rounds("B", "C", count=8)
        assert takeovers(cl["B"]) == []

    asyncio.run(scenario())


def test_rechazo_de_una_ficha_de_a_caido_lo_replanea_b(tmp_path) -> None:
    async def scenario() -> None:
        cl = Cluster(tmp_path)
        cl["A"].receive_message(REPORT)
        await cl.rounds(count=3)
        cl.kill("A")
        (card,) = incident_cards(cl["C"].store.since({}))
        assert card.ficha_id.startswith("A:")
        request = DecisionRequest(ficha_id=card.ficha_id, action="rechazar", operator="cmd")
        cl["C"].decide(request)
        await cl.rounds("B", "C", count=7)
        (after,) = incident_cards(cl["C"].store.since({}))
        assert after.ficha_id.startswith("B:") and after.status == "pendiente"
        assert "replaneada" in after.suggestion

    asyncio.run(scenario())


def test_cae_c_y_lo_toma_b(tmp_path) -> None:
    async def scenario() -> None:
        cl = Cluster(tmp_path)
        received_only(cl["C"])
        await cl.spread()
        cl.kill("C")
        await cl.rounds("A", "B", count=7)
        (event,) = takeovers(cl["A"])
        assert event.origin_node == "B" and derived_by(cl["A"])["ficha"] == {"B"}

    asyncio.run(scenario())


# --- A vuelve y converge ------------------------------------------------------------------


def test_a_vuelve_desde_su_sqlite_converge_y_no_duplica(tmp_path) -> None:
    async def scenario() -> None:
        cl = Cluster(tmp_path)
        received_only(cl["A"])
        await cl.spread()
        cl.kill("A")
        await cl.rounds("B", "C", count=7)
        a = cl.revive("A")
        assert a.resilience.stats.restarted and not a.resilience.ready()
        assert a.next_seq() == 2  # el consecutivo sigue desde la SQLite
        await cl.rounds(count=3)
        assert converged(cl.alive("A", "B", "C"))
        assert a.resilience.stats.recovery_s is not None
        assert not a.owns(("A", 1))  # ya es de B: A no lo repite
        assert derived_by(a)["ficha"] == {"B"} and a.resilience.stats.resumed == 0
        # Lo nuevo que reciba A vuelve a ser suyo
        a.receive_message(message("se cayo un muro", seq=9))
        assert derived_by(a, "a1b2c3d4-9")["ficha"] == {"A"}

    asyncio.run(scenario())


def test_a_vuelve_antes_del_timeout_y_retoma_lo_suyo(tmp_path) -> None:
    async def scenario() -> None:
        cl = Cluster(tmp_path)
        received_only(cl["A"])
        await cl.spread()
        cl.kill("A")
        await cl.rounds("B", "C", count=2)  # 4 s: B todavía no lo da por caído
        a = cl.revive("A")
        await cl.rounds(count=3)
        assert takeovers(cl["B"]) == [] and a.resilience.stats.resumed >= 1
        assert derived_by(cl["C"])["ficha"] == {"A"}

    asyncio.run(scenario())


def test_measure_failover_mide_caida_toma_y_recuperacion(tmp_path) -> None:
    async def scenario() -> None:
        cl = Cluster(tmp_path, LORA.with_loss(0.3), seed=3)
        received_only(cl["A"])
        received_only(cl["A"], message("se cayo un muro", seq=9))
        await cl.spread(count=20)
        result = await measure_failover(
            cl.nodes, cl.network, cl.clock, victim="A", restart=lambda: cl.start("A")
        )
        assert result.taker == "B" and result.pending_before == 2 and result.pending_after == 0
        assert result.detection_s > cl["B"].resilience.timeout
        assert result.takeover_s >= result.detection_s
        assert result.recovery_s is not None and result.converge_s is not None
        assert converged(cl.nodes.values()) and pending_of(cl["C"], "A") == 0

    asyncio.run(scenario())


# --- Partición: A aislado pero vivo -------------------------------------------------------


def test_particion_los_dos_procesan_y_la_tarjeta_sale_una_vez(tmp_path) -> None:
    async def scenario() -> None:
        cl = Cluster(tmp_path)
        received_only(cl["A"])
        await cl.spread()
        cl.kill("A")  # A sigue vivo, solo, y no oye a nadie
        await cl.rounds("A", count=7)
        await cl.rounds("B", "C", count=7)
        assert derived_by(cl["A"])["ficha"] == {"A"} and derived_by(cl["B"])["ficha"] == {"B"}
        for peer in PEERS["A"]:
            cl.network.connect("A", peer)
        await cl.rounds(count=4)
        assert converged(cl.alive("A", "B", "C"))
        assert cl["A"].ownership.owner_of(("A", 1)) == "B"  # todos concluyen lo mismo
        (card,) = incident_cards(cl["A"].store.since({}))
        assert card.report_count == 1
        detail = incident_detail(cl["A"].store.since({}), card.incident_id)
        assert any("ficha paralela" in s.detail for s in detail.steps)

    asyncio.run(scenario())


def test_un_reporte_agrupado_por_dos_nodos_va_en_un_solo_incidente() -> None:
    a = Node("A", SQLiteEventStore(), clock=lambda: NOW, classifier=None)
    a.receive_message(REPORT)
    events = a.store.since({})
    (group,) = [e for e in events if e.type == "incidente_agrupado"]
    other = group.model_copy(
        update={"origin_node": "B", "seq": 1, "received_at": NOW + 1,
                "payload": {**group.payload, "incident_id": "otro"}}
    )  # fmt: skip
    cards = incident_cards([*events, other])
    assert [c.incident_id for c in cards] == ["a1b2c3d4-7"]


# --- Triaje cortado (pendiente de N8) -----------------------------------------------------


def test_al_volver_retoma_el_triaje_que_se_corto(tmp_path) -> None:
    path = tmp_path / "A.db"
    before = Node("A", SQLiteEventStore(path), clock=lambda: NOW, classifier=GrayClassifier())
    before.receive_message(message(GATO))  # sin LLM: N7 lo deja "ambiguo, revisar"
    assert [w.kind for w in pending_work(before.store.since({}))] == ["triaje"]
    llm = ScriptedLLM([final(2, quote="gato herido")])
    after = Node("A", SQLiteEventStore(path), clock=lambda: NOW, classifier=GrayClassifier(),
                 llm=llm)  # fmt: skip
    (work,) = after.resilience.check()
    assert work.kind == "triaje" and after.resilience.stats.resumed == 1
    agent = [e for e in after.store.since({}) if e.payload.get("source") == "agente"]
    assert len(agent) == 1 and agent[0].refs == (work.event.id,)
    assert after.resilience.check() == []  # no se repite


def test_un_triaje_que_ya_no_le_toca_no_se_publica(tmp_path) -> None:
    a = Node("A", SQLiteEventStore(), clock=lambda: NOW, classifier=GrayClassifier())
    a.receive_message(message(GATO))
    (urgency,) = [e for e in a.store.since({}) if e.type == "urgencia_estimada"]
    a.triaje = __import__("guadua.agents.triaje", fromlist=["x"]).AgenteTriaje(
        ScriptedLLM([final(2, quote="gato herido")])
    )
    case = a.triaje.prepare(urgency, a.store)
    a.record(reasignacion("B", 9, "A", a._seq))  # B tomó lo de A mientras el agente pensaba
    a._publish_triage(case, a.triaje.run(case))
    assert [e.payload["source"] for e in a.store.since({}) if e.type == "urgencia_estimada"] == [
        "clasificador"
    ]


# --- Nunca tumba el nodo ------------------------------------------------------------------


def test_un_error_de_resiliencia_no_tumba_el_nodo(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        cl = Cluster(tmp_path)

        def broken(*args, **kwargs):
            raise RuntimeError("falla")

        monkeypatch.setattr(resilience, "pending_work", broken)
        await cl.rounds(count=8)  # cada ronda de resiliencia falla
        assert cl["A"].receive_message(REPORT).duplicate is False
        await cl.rounds(count=3)
        assert converged(cl.alive("A", "B", "C"))

    asyncio.run(scenario())


def test_peer_timeout_desde_el_entorno() -> None:
    assert NodeConfig.from_env({"SARD_PEER_TIMEOUT": "6"}).peer_timeout == 6.0
    assert NodeConfig.from_env({}).peer_timeout == 10.0


@pytest.mark.parametrize("bad", [{"SARD_PEER_TIMEOUT": "x"}])
def test_peer_timeout_invalido(bad) -> None:
    from guadua.node.config import ConfigError

    with pytest.raises(ConfigError):
        NodeConfig.from_env(bad)
