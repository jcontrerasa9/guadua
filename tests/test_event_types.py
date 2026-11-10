import json
from pathlib import Path

import pytest

from guadua.store.event_types import CATALOG, EventTypeError, report_id, validate_event
from guadua.store.events import Event

EXAMPLES = json.loads((Path(__file__).parent / "fixtures" / "event_examples.json").read_text())


def to_event(data: dict) -> Event:
    return Event.model_validate({**data, "refs": [tuple(r) for r in data["refs"]]})


@pytest.mark.parametrize("case", EXAMPLES["valid"], ids=lambda c: c["type"])
def test_valid_examples_pass(case: dict) -> None:
    payload = validate_event(to_event(case))
    assert isinstance(payload, CATALOG[case["type"]][1])


@pytest.mark.parametrize("case", EXAMPLES["invalid"], ids=lambda c: c["name"])
def test_invalid_examples_are_rejected(case: dict) -> None:
    with pytest.raises(EventTypeError):
        validate_event(to_event(case["event"]))


def test_every_type_has_an_example() -> None:
    assert {case["type"] for case in EXAMPLES["valid"]} == set(CATALOG)


def test_report_id_format() -> None:
    assert report_id(0xA1B2C3D4, 7) == "a1b2c3d4-7"
    assert report_id(42, 2) == "0000002a-2"
