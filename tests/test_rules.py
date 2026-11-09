"""Reglas de prioridad mínima (N7, capa 1): señales vitales → piso crítico, en < 1 ms."""

import time

import pytest

from guadua.classifier import rules
from guadua.classifier.rules import apply_rules, fold
from guadua.classifier.urgency import Urgency
from guadua.protocol.header import Flag, Incident, MessageType, People
from guadua.store.event_types import Reporte


def reporte(
    text: str | None = None,
    *,
    incident: Incident = Incident.OTRO,
    flags: int = 0,
    msg_type: MessageType = MessageType.REPORTE,
) -> Reporte:
    return Reporte(
        report_id="a1b2c3d4-7",
        msg_type=msg_type,
        incident=incident,
        people=People.NO_SE,
        flags=int(flags),
        barrio=0,
        device_time=0,
        text=text,
        channel="wifi",
        raw_hex="",
    )


def test_fold_quita_tildes_y_mayusculas() -> None:
    assert fold("ATRAPADÓS en el Pingüino, Ñame") == "atrapados en el pinguino, name"


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("hay 3 atrapados en la casa", rules.ATRAPADOS),
        ("quedaron ATRAPADAS", rules.ATRAPADOS),
        ("mi hermano esta atrapao", rules.ATRAPADOS),
        ("hay gente debajo de los escombros", rules.ATRAPADOS),
        ("quedó bajo la placa", rules.ATRAPADOS),
        ("el señor no respira", rules.NO_RESPIRA),
        ("dejó de respirar", rules.NO_RESPIRA),
        ("está sangrando mucho", rules.SANGRADO),
        ("hay mucha sangre", rules.SANGRADO),
        ("la señora está inconsciente", rules.INCONSCIENTE),
        ("se desmayó y no reacciona", rules.INCONSCIENTE),
        ("Incendio en el edificio", rules.INCENDIO),
        ("la casa está en llamas", rules.INCENDIO),
    ],
)
def test_senales_vitales_en_el_texto_fijan_critica(text: str, rule: str) -> None:
    result = apply_rules(reporte(text))
    assert result.floor == Urgency.CRITICA
    assert rule in result.rules


@pytest.mark.parametrize(
    "text",
    [
        "se vino abajo el muro, no hay heridos",  # "abajo el muro" no es "bajo el muro"
        "se cayó un árbol y tapó la calle",
        "todos estamos bien",
        "se quemó un bombillo",
    ],
)
def test_sin_senales_vitales_no_hay_piso(text: str) -> None:
    assert apply_rules(reporte(text)) == rules.NO_RULES


def test_la_cabecera_tambien_fija_critica() -> None:
    assert apply_rules(reporte(msg_type=MessageType.SOS_ESCOMBROS)).rules == (rules.SOS,)
    assert apply_rules(reporte(flags=Flag.ATRAPADOS)).rules == (rules.ATRAPADOS,)
    assert apply_rules(reporte(incident=Incident.ATRAPADOS)).rules == (rules.ATRAPADOS,)
    assert apply_rules(reporte(flags=Flag.NO_RESPIRA_O_SANGRADO)).rules == (
        rules.NO_RESPIRA_O_SANGRADO,
    )
    assert apply_rules(reporte(flags=Flag.INCENDIO)).rules == (rules.INCENDIO,)
    assert apply_rules(reporte(incident=Incident.INCENDIO)).rules == (rules.INCENDIO,)


def test_otras_banderas_no_suben_el_piso() -> None:
    flags = Flag.RIESGO_ESCENA | Flag.OBSERVACION_DIRECTA | Flag.GPS | Flag.BATERIA_CRITICA
    assert apply_rules(reporte(flags=flags, incident=Incident.FUGA_GAS)) == rules.NO_RULES


def test_reglas_sin_repetir_y_en_orden_fijo() -> None:
    result = apply_rules(
        reporte(
            "incendio, hay atrapados y uno no respira; otro atrapado",
            flags=Flag.ATRAPADOS,
            msg_type=MessageType.SOS_ESCOMBROS,
        )
    )
    assert result.rules == (rules.SOS, rules.ATRAPADOS, rules.NO_RESPIRA, rules.INCENDIO)


def test_no_interpreta_negaciones() -> None:
    """Prefiere equivocarse hacia arriba: bajar un caso es decisión de una persona."""
    assert apply_rules(reporte("no hay atrapados")).floor == Urgency.CRITICA


def test_reglas_en_menos_de_1_ms() -> None:
    sample = reporte(
        "URGENTE se cayo la casa en El Cable, mi abuela esta debajo de la placa y no respira"
    )
    times = []
    for _ in range(500):
        start = time.perf_counter()
        apply_rules(sample)
        times.append(time.perf_counter() - start)
    times.sort()
    assert times[len(times) // 2] < 0.001
