"""Señales de riesgo para rescatistas (D3a): se marcan, pero no cambian la urgencia."""

import csv

import pytest

from guadua.classifier.risk import ORDER, detect_risk_signals
from guadua.classifier.triage import Prediction, triage
from guadua.classifier.urgency import Urgency
from guadua.node.pipeline import normalize
from guadua.node.views import RISK_LABELS
from guadua.protocol.header import Flag, Incident, MessageType
from tests.test_rules import reporte


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Huele a GAS en el edificio", ("gas",)),
        ("hay una fuga de propano", ("gas",)),
        ("los cables quedaron caídos en la calle", ("cables_caidos",)),
        ("cable de alta tension en el piso", ("cables_caidos",)),
        ("se cayó el poste y hay chispas", ("cables_caidos",)),
        ("hay pipetas expuestas en el patio", ("cilindros_expuestos",)),
        ("un cilindro rodando", ("cilindros_expuestos",)),
        ("la casa tiene grietas y está a punto de caerse", ("colapso_secundario",)),
        ("con la replica se siguen cayendo los muros", ("colapso_secundario",)),
        ("la vía está bloqueada por el derrumbe", ("via_bloqueada",)),
        ("un arbol caido tapó la calle, no hay paso", ("via_bloqueada",)),
        ("la casa está en llamas", ("incendio",)),
        (
            "olor a gas, cables sueltos y la pared inestable",
            ("gas", "cables_caidos", "colapso_secundario"),
        ),
    ],
)
def test_detecta_en_el_texto(text: str, expected: tuple[str, ...]) -> None:
    assert detect_risk_signals(reporte(text)) == expected


@pytest.mark.parametrize(
    "text",
    [
        "señora con la pierna fracturada",
        "gaseosa derramada en la tienda",  # "gas" solo como palabra completa
        "no hay pastillas para la tensión",
        None,
    ],
)
def test_sin_riesgo(text: str | None) -> None:
    assert detect_risk_signals(reporte(text)) == ()


def test_cabecera_banderas_y_tipo() -> None:
    assert detect_risk_signals(reporte(flags=Flag.INCENDIO)) == ("incendio",)
    assert detect_risk_signals(reporte(flags=Flag.RIESGO_ESCENA)) == ("gas_o_cables",)
    assert detect_risk_signals(reporte(incident=Incident.FUGA_GAS)) == ("gas",)
    assert detect_risk_signals(reporte(incident=Incident.VIA_BLOQUEADA)) == ("via_bloqueada",)
    assert detect_risk_signals(reporte(incident=Incident.INCENDIO)) == ("incendio",)
    sos = reporte(msg_type=MessageType.SOS_ESCOMBROS)
    assert detect_risk_signals(sos) == ()


def test_el_texto_precisa_la_bandera_gas_o_cables() -> None:
    assert detect_risk_signals(reporte("huele a gas", flags=Flag.RIESGO_ESCENA)) == ("gas",)
    assert detect_risk_signals(reporte("cables caidos", flags=Flag.RIESGO_ESCENA)) == (
        "cables_caidos",
    )
    # Si el texto no dice cuál, queda la bandera tal cual.
    assert detect_risk_signals(reporte("pared inestable", flags=Flag.RIESGO_ESCENA)) == (
        "gas_o_cables",
        "colapso_secundario",
    )


def test_sin_repetir_y_en_orden_fijo() -> None:
    r = reporte("fuego y huele a gas, hay fuego", incident=Incident.INCENDIO, flags=Flag.INCENDIO)
    assert detect_risk_signals(r) == ("gas", "incendio")


def test_todas_las_senales_tienen_unidad_y_etiqueta() -> None:
    """Coordinación (D3b) y la tarjeta (D3c) saben qué hacer con cada señal."""
    with open("data/riesgos_unidad.csv", encoding="utf-8") as f:
        catalog = {row["senal_riesgo"] for row in csv.DictReader(f)}
    assert set(ORDER) <= catalog
    assert set(ORDER) <= set(RISK_LABELS)


def test_la_normalizacion_las_marca() -> None:
    assert normalize(reporte("hay cables caidos")).risk_signals == ("cables_caidos",)


# --- Invariante: el riesgo no cambia la urgencia ------------------------------------------


class FixedClassifier:
    """Ignora el texto: así solo varía lo que agrega la frase de riesgo."""

    def __init__(self, level: Urgency) -> None:
        self.level = level

    def predict(self, incident, text) -> Prediction:
        return Prediction(self.level, {"critica": 0.1, "alta": 0.7, "media": 0.1, "baja": 0.1})


@pytest.mark.parametrize("classifier", [None, FixedClassifier(Urgency.MEDIA)])
@pytest.mark.parametrize(
    "base", ["señora con la pierna fracturada", "atrapados bajo la placa", "hay un herido"]
)
def test_misma_urgencia_con_y_sin_riesgo(base: str, classifier) -> None:
    risky = f"{base}, huele a gas, cables caidos y la casa esta a punto de caerse"
    plain, marked = reporte(base), reporte(risky)
    without = triage(plain, classifier, detect_risk_signals(plain))
    with_risk = triage(marked, classifier, detect_risk_signals(marked))
    assert with_risk.risk_signals == ("gas", "cables_caidos", "colapso_secundario")
    assert without.risk_signals == ()
    assert (with_risk.level, with_risk.floor, with_risk.rules, with_risk.source) == (
        without.level,
        without.floor,
        without.rules,
        without.source,
    )


def test_un_critico_con_gas_sigue_critico_y_marcado() -> None:
    r = reporte("atrapados y huele a gas")
    out = triage(r, None, detect_risk_signals(r))
    assert (out.level, out.floor, out.risk_signals) == (Urgency.CRITICA, Urgency.CRITICA, ("gas",))
