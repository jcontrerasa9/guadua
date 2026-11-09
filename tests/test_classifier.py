"""Datos sintéticos, clasificador calibrado y triaje (N7, capas 1 y 2)."""

import time

import pytest

from guadua.classifier import synthetic
from guadua.classifier.evaluate import as_reporte, evaluate, render
from guadua.classifier.rules import ATRAPADOS
from guadua.classifier.synthetic import DATA_PATH, generate, load
from guadua.classifier.triage import (
    PROBABILITY_KEYS,
    Prediction,
    default_classifier,
    in_gray_zone,
    triage,
)
from guadua.classifier.urgency import Urgency
from guadua.protocol.header import Flag, Incident
from guadua.store.event_types import UrgenciaEstimada
from tests.test_rules import reporte


@pytest.fixture(scope="module")
def classifier():
    trained = default_classifier()
    assert trained is not None, "el extra ml (scikit-learn) va incluido en dev"
    return trained


@pytest.fixture(scope="module")
def rows() -> list[synthetic.Row]:
    return load()


class FixedClassifier:
    def __init__(self, level: Urgency, probabilities: dict[str, float]) -> None:
        self.prediction = Prediction(level, probabilities)

    def predict(self, incident: Incident, text: str | None) -> Prediction:
        return self.prediction


class BrokenClassifier:
    def predict(self, incident: Incident, text: str | None) -> Prediction:
        raise RuntimeError("modelo dañado")


CONFIDENT_HIGH = {"alta": 0.9, "media": 0.06, "critica": 0.03, "baja": 0.01}
UNSURE = {"media": 0.4, "alta": 0.35, "baja": 0.15, "critica": 0.1}


# --- Datos sintéticos ---------------------------------------------------------------------


def test_el_csv_versionado_es_el_que_sale_de_la_semilla(rows) -> None:
    assert generate() == rows, f"regenera {DATA_PATH} con python -m guadua.classifier.synthetic"


def test_train_y_test_no_comparten_textos_ni_pares(rows) -> None:
    by_split = {s: [r for r in rows if r.split == s] for s in ("train", "test", "ambiguo")}
    texts = {s: {r.text for r in rs} for s, rs in by_split.items()}
    assert not texts["train"] & texts["test"]
    assert not texts["train"] & texts["ambiguo"]
    assert not {r.template for r in by_split["train"]} & {r.template for r in by_split["test"]}


def test_los_cuatro_niveles_estan_balanceados_en_cada_conjunto(rows) -> None:
    for split in ("train", "test"):
        counts = [sum(r.split == split and r.label == lvl for r in rows) for lvl in Urgency]
        assert min(counts) >= 0.9 * max(counts) > 0


def test_hay_casos_ambiguos_para_n8(rows) -> None:
    assert sum(r.split == "ambiguo" for r in rows) >= 100


# --- Clasificador -------------------------------------------------------------------------


def test_probabilidades_de_los_cuatro_niveles(classifier) -> None:
    prediction = classifier.predict(Incident.COLAPSO, "se cayo la casa y hay 3 atrapados")
    assert set(prediction.probabilities) == set(PROBABILITY_KEYS.values())
    assert sum(prediction.probabilities.values()) == pytest.approx(1, abs=1e-3)
    assert prediction.level == Urgency.CRITICA


def test_aguanta_faltas_de_ortografia(classifier) -> None:
    """Las reglas no ven "atrapdos"; los n-gramas de caracteres sí."""
    text = "se cayo la casa en Chipre y hay dos persnas atrapdas"
    assert classifier.predict(Incident.COLAPSO, text).level == Urgency.CRITICA


def test_mira_el_texto_y_no_solo_la_categoria(classifier) -> None:
    """Marcar "Herido" no basta: un animal herido no es urgente para el triaje de personas."""
    assert classifier.predict(Incident.HERIDO, "Hay un gato herido").level <= Urgency.MEDIA
    text = "mi papa tiene una pierna fracturada"
    assert classifier.predict(Incident.OTRO, text).level >= Urgency.ALTA


@pytest.mark.parametrize(
    "text",
    ["Hay una tortuga herida", "mi hijo tiene fiebre alta", "hubo una explosion en la fabrica"],
)
def test_texto_desconocido_queda_ambiguo(classifier, text: str) -> None:
    """El clasificador no entiende palabras que nunca vio: lo revisa una persona."""
    assert classifier.predict(Incident.HERIDO, text).unfamiliar
    assert triage(reporte(text, incident=Incident.HERIDO), classifier).ambiguous


def test_faltas_de_ortografia_no_son_texto_desconocido(classifier) -> None:
    vocabulary = classifier.vocabulary
    assert vocabulary.unknown("se cayo la csa y hay dos persnas atrapdas") == []
    assert vocabulary.unknown("Hay una TORTUGA herida") == ["tortuga"]
    assert vocabulary.unknown_ratio("Hay una tortuga herida") == pytest.approx(0.25)
    assert vocabulary.unknown_ratio(None) == 0.0


def test_reporte_sin_texto_tambien_se_clasifica(classifier) -> None:
    assert classifier.predict(Incident.VIA_BLOQUEADA, None).level in Urgency


def test_es_determinista(classifier) -> None:
    from guadua.classifier.model import CalibratedClassifier

    again = CalibratedClassifier.train(load())
    text = "se cayo el techo y mi abuela no se mueve"
    assert again.threshold == classifier.threshold
    assert again.predict(Incident.COLAPSO, text) == classifier.predict(Incident.COLAPSO, text)


def test_clasificador_en_menos_de_2_ms(classifier) -> None:
    times = []
    for _ in range(200):
        start = time.perf_counter()
        classifier.predict(Incident.HERIDO, "mi papa se golpeo la cabeza pero habla, en Chipre")
        times.append(time.perf_counter() - start)
    times.sort()
    assert times[len(times) // 2] < 0.002


def test_sensibilidad_en_criticos_con_el_conjunto_de_prueba(rows, classifier) -> None:
    """Guarda contra retrocesos. La medición completa: python -m guadua.classifier.evaluate."""
    report = evaluate(rows, classifier)
    assert report.test_size == sum(r.split == "test" for r in rows)
    assert report.system.sensitivity >= 0.90  # medido: 94,4 %
    assert report.system.precision >= 0.90  # medido: 97,1 %
    assert report.ambiguous_rate <= 0.20  # medido: 14,1 %; no todo puede ir a revisión
    assert report.rules_ms[0] < 1 and report.classifier_ms[0] < 2
    assert "Matriz de confusión" in render(report)


# --- Triaje: reglas + clasificador --------------------------------------------------------


def test_las_reglas_fijan_el_piso_y_el_clasificador_no_lo_baja() -> None:
    low = FixedClassifier(
        Urgency.BAJA, {"baja": 0.97, "media": 0.01, "alta": 0.01, "critica": 0.01}
    )
    result = triage(reporte("hay atrapados"), low)
    assert (result.level, result.floor, result.source) == (
        Urgency.CRITICA,
        Urgency.CRITICA,
        "reglas",
    )
    assert result.rules == (ATRAPADOS,)
    assert result.probabilities["baja"] == 0.97
    assert not result.ambiguous


def test_el_clasificador_sube_el_nivel_pero_no_el_piso() -> None:
    """Lo que agrega el clasificador es sugerencia: N8 podrá bajarlo hasta el piso."""
    result = triage(
        reporte("mi papa se cayo de la escalera"), FixedClassifier(Urgency.ALTA, CONFIDENT_HIGH)
    )
    assert (result.level, result.floor, result.source) == (
        Urgency.ALTA,
        Urgency.BAJA,
        "clasificador",
    )
    assert not result.ambiguous


def test_si_el_nivel_sale_del_clasificador_la_fuente_es_el_clasificador() -> None:
    low = FixedClassifier(Urgency.BAJA, {"baja": 0.9, "media": 0.08, "alta": 0.01, "critica": 0.01})
    result = triage(reporte("hay un gato herido"), low)
    assert (result.level, result.source) == (Urgency.BAJA, "clasificador")


def test_zona_gris_queda_ambigua_para_revision() -> None:
    result = triage(reporte("algo raro en la casa"), FixedClassifier(Urgency.MEDIA, UNSURE))
    assert result.level == Urgency.MEDIA
    assert result.ambiguous


def test_texto_desconocido_es_ambiguo_aunque_el_clasificador_este_seguro() -> None:
    sure = FixedClassifier(Urgency.ALTA, CONFIDENT_HIGH)
    sure.prediction = Prediction(Urgency.ALTA, CONFIDENT_HIGH, unfamiliar=True)
    result = triage(reporte("hay una tortuga"), sure)
    assert result.level == Urgency.ALTA and result.ambiguous  # no baja: lo revisa alguien
    with_rule = triage(reporte("tortuga atrapada"), sure)
    assert with_rule.level == Urgency.CRITICA and not with_rule.ambiguous  # la regla manda


def test_zona_gris() -> None:
    assert in_gray_zone(UNSURE)  # máxima < 0,5
    assert in_gray_zone({"alta": 0.55, "media": 0.45})  # parejas
    assert in_gray_zone({})
    assert not in_gray_zone(CONFIDENT_HIGH)


def test_si_falla_el_clasificador_quedan_las_reglas() -> None:
    result = triage(reporte("hay atrapados"), BrokenClassifier())
    assert (result.level, result.source, result.probabilities) == (Urgency.CRITICA, "reglas", {})
    alone = triage(reporte("se cayo un arbol"), BrokenClassifier())
    assert (alone.level, alone.source) == (Urgency.BAJA, "reglas")
    assert alone.ambiguous  # sin reglas ni clasificador: lo revisa una persona


def test_sin_clasificador_quedan_las_reglas() -> None:
    assert triage(reporte(flags=Flag.ATRAPADOS), None).level == Urgency.CRITICA
    assert triage(reporte("todo bien"), None).ambiguous


def test_copia_las_senales_de_riesgo_y_cumple_el_catalogo(rows, classifier) -> None:
    result = triage(reporte("huele a gas"), classifier, risk_signals=("gas_o_cables",))
    assert result.risk_signals == ("gas_o_cables",)
    for i, row in enumerate(r for r in rows if r.split == "test"):
        estimate = triage(as_reporte(row, i), classifier)
        UrgenciaEstimada.model_validate(estimate.model_dump(mode="json"))
        assert estimate.level >= estimate.floor  # regla 1
        assert estimate.floor == (Urgency.CRITICA if estimate.rules else Urgency.BAJA)
