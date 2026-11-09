"""Pruebas de cómo se arman las tarjetas a partir de los eventos (node/views.py)."""

import json
from pathlib import Path

from guadua.classifier.urgency import Urgency
from guadua.node.views import PROVISIONAL_STEP, incident_cards, incident_detail
from guadua.store.events import Event

EXAMPLES = json.loads(
    (Path(__file__).parent / "fixtures" / "event_examples.json").read_text(encoding="utf-8")
)
EVENTS = [Event.model_validate(e) for e in EXAMPLES["valid"]]
BY_TYPE = {e.type: e for e in EVENTS}


def derived(base: Event, seq: int, **payload) -> Event:
    return base.model_copy(update={"seq": seq, "payload": {**base.payload, **payload}})


def report(rid: str, seq: int, received_at: float, **payload) -> list[Event]:
    """Un reporte con su normalizado, de otro dispositivo y otra hora."""
    reporte = BY_TYPE["reporte"].model_copy(
        update={"seq": seq, "received_at": received_at,
                "payload": {**BY_TYPE["reporte"].payload, "report_id": rid, **payload}}
    )  # fmt: skip
    normalized = BY_TYPE["reporte_normalizado"].model_copy(
        update={"seq": seq + 1, "received_at": received_at, "refs": (reporte.id,),
                "payload": {**BY_TYPE["reporte_normalizado"].payload, "report_id": rid}}
    )  # fmt: skip
    return [reporte, normalized]


def urgency(rid: str, seq: int, level: int, floor: int | None = None) -> Event:
    return derived(
        BY_TYPE["urgencia_estimada"], seq, report_id=rid, level=level,
        floor=level if floor is None else floor,
    )  # fmt: skip


def test_example_events_make_the_example_card() -> None:
    (card,) = incident_cards(EVENTS)
    assert card.incident_id == "a1b2c3d4-7"
    assert card.level == Urgency.CRITICA
    assert card.status == "aprobada"  # decisión A:58 sobre la ficha B:31
    assert card.ficha_id == "B:31"
    assert card.suggestion == "Bomberos Manizales (2 unidades libres)"
    assert card.location.label == "El Cable"
    assert card.confidence == 0.6


def test_order_of_arrival_does_not_matter() -> None:
    assert incident_cards(reversed(EVENTS)) == incident_cards(EVENTS)


def test_level_never_drops_takes_highest_urgency() -> None:
    """Regla 1: tras una partición pueden quedar dos urgencias; vale la mayor."""
    events = report("0000beef-1", 10, 100.0) + [
        urgency("0000beef-1", 12, Urgency.CRITICA),
        urgency("0000beef-1", 13, Urgency.MEDIA),  # más reciente, pero menor
    ]
    (card,) = incident_cards(events)
    assert card.level == Urgency.CRITICA


def test_no_triage_shows_provisional_critical() -> None:
    (card,) = incident_cards(report("0000beef-1", 10, 100.0))
    assert card.level == Urgency.CRITICA
    detail = incident_detail(report("0000beef-1", 10, 100.0), "0000beef-1")
    assert detail.steps[0] == PROVISIONAL_STEP


def test_ungrouped_report_is_its_own_incident() -> None:
    (card,) = incident_cards(report("0000beef-1", 10, 100.0))
    assert card.incident_id == "0000beef-1"  # el report_id mientras no haya correlación
    assert card.confidence is None
    assert card.status == "sin_ficha" and card.ficha_id is None


def test_report_without_normalized_yet_still_shows() -> None:
    reporte = report("0000beef-1", 10, 100.0)[0]
    (card,) = incident_cards([reporte])
    assert card.incident == 1 and card.location.geohash == "d2fkcdc"


def test_conflicting_decisions_mark_conflict() -> None:
    reject = derived(BY_TYPE["decision"], 59, action="rechazar")
    (card,) = incident_cards([*EVENTS, reject])
    assert card.status == "conflicto"


def test_edited_decision_shows_edited_message() -> None:
    events = [e for e in EVENTS if e.type != "decision"]
    edit = derived(BY_TYPE["decision"], 59, action="editar", edited_message="Mensaje nuevo")
    (card,) = incident_cards([*events, edit])
    assert card.status == "editada" and card.message == "Mensaje nuevo"


def test_cards_sorted_by_level_then_age() -> None:
    events = (
        report("00000001-1", 10, 300.0) + [urgency("00000001-1", 20, Urgency.MEDIA)]
        + report("00000002-1", 30, 200.0) + [urgency("00000002-1", 40, Urgency.ALTA)]
        + report("00000003-1", 50, 100.0) + [urgency("00000003-1", 60, Urgency.ALTA)]
    )  # fmt: skip
    cards = incident_cards(events)
    assert [c.incident_id for c in cards] == ["00000003-1", "00000002-1", "00000001-1"]


def test_grouped_reports_count_and_sources() -> None:
    events = report("00000001-1", 10, 100.0) + report("00000002-1", 20, 160.0)
    group = derived(
        BY_TYPE["incidente_agrupado"], 30, incident_id="00000001-1",
        report_ids=["00000001-1", "00000002-1"], link="seguro", confidence=0.9,
    )  # fmt: skip
    (card,) = incident_cards([*events, group])
    assert card.report_count == 2 and card.received_at == 100.0
    detail = incident_detail([*events, group], "00000001-1")
    assert detail.report_ids == ("00000001-1", "00000002-1")
    assert detail.sources == ("ciudadano directo", "ciudadano directo")
    assert incident_detail([*events, group], "00000002-1") is None  # quedó dentro del grupo
