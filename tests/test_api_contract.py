import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from guadua.node.api import ROUTES, SCHEMAS

EXAMPLES = json.loads((Path(__file__).parent / "fixtures" / "api_examples.json").read_text())


@pytest.mark.parametrize("case", EXAMPLES["valid"], ids=lambda c: c["route"])
def test_valid_examples_match_schema(case: dict) -> None:
    model = SCHEMAS[case["schema"]].model_validate(case["body"])
    assert model.model_dump(mode="json", exclude_unset=True) == case["body"]


@pytest.mark.parametrize("case", EXAMPLES["invalid"], ids=lambda c: c["name"])
def test_invalid_examples_are_rejected(case: dict) -> None:
    with pytest.raises(ValidationError):
        SCHEMAS[case["schema"]].model_validate(case["body"])


def test_every_response_schema_has_an_example() -> None:
    covered = {case["schema"] for case in EXAMPLES["valid"]}
    for route in ROUTES:
        for name in route.response.split(" | "):
            assert name in covered, f"falta ejemplo de {name} ({route.method} {route.path})"


def test_routes_are_unique_and_under_prefix() -> None:
    keys = [(r.method, r.path) for r in ROUTES]
    assert len(keys) == len(set(keys))
    assert all(r.path.startswith("/api/") for r in ROUTES)
