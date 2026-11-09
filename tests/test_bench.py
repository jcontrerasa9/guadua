"""Banco de fallos (N15): un comando produce la tabla de métricas de §15 con semillas fijas.

Las pruebas usan la configuración rápida (LoRa 0 y 60 %, menos reportes) para no tardar.
"""

import asyncio

import pytest

from guadua.bench.cli import QUICK, main, run
from guadua.bench.escenarios import channels, percentile, sample_reports
from guadua.bench.tabla import CPU_NOTE, render
from guadua.channel.profiles import LORA, SMS
from guadua.classifier.synthetic import load
from guadua.classifier.triage import default_classifier
from guadua.classifier.urgency import Urgency


def without_cpu_times(table: str) -> str:
    return "\n".join(line for line in table.splitlines() if CPU_NOTE not in line)


@pytest.fixture(scope="module")
def results():
    return asyncio.run(run(7, QUICK))


@pytest.fixture(scope="module")
def table(results) -> str:
    return render(results)


# --- Piezas -------------------------------------------------------------------------------


def test_percentil_por_rango_mas_cercano() -> None:
    assert percentile([], 0.5) is None
    assert percentile([5.0], 0.95) == 5.0
    assert percentile([4.0, 1.0, 3.0, 2.0], 0.5) == 2.0
    assert percentile(list(map(float, range(1, 101))), 0.95) == 95.0


def test_filas_de_canal_sin_cortes_y_con_el_perfil_tal_cual() -> None:
    rows = channels([LORA, SMS], [0.0, 0.3])
    assert [(c.name, c.label) for c in rows] == [
        ("lora", "0 %"), ("lora", "30 %"), ("lora", "cortes"),
        ("sms", "0 %"), ("sms", "30 %"), ("sms", "cortes"),
    ]  # fmt: skip
    assert rows[1].profile.loss == 0.3 and rows[1].profile.cut_rate == 0.0
    assert rows[1].profile.expected_loss == pytest.approx(0.3)
    assert rows[2].profile == LORA
    assert all(c.label != "cortes" for c in channels([LORA], [0.0], cuts=False))


def test_muestra_repartida_por_urgencia_y_fija_con_la_semilla() -> None:
    rows, classifier = load(), default_classifier()
    a = sample_reports(rows, classifier, 20, seed=3)
    assert a == sample_reports(rows, classifier, 20, seed=3)
    assert a != sample_reports(rows, classifier, 20, seed=4)
    assert len({(s.device_id, s.seq) for s in a}) == 20
    assert all(len(s.payload) <= 121 for s in a)  # 21 B de cabecera + ≤ 100 B de texto
    assert {s.urgency for s in a} >= {Urgency.CRITICA, Urgency.BAJA}


# --- Lo que mide ----------------------------------------------------------------------------


def test_misma_semilla_misma_tabla(table) -> None:
    again = render(asyncio.run(run(7, QUICK)))
    assert without_cpu_times(again) == without_cpu_times(table)


def test_otra_semilla_cambia_lo_que_depende_del_canal(table) -> None:
    other = render(asyncio.run(run(8, QUICK)))
    assert without_cpu_times(other) != without_cpu_times(table)


def test_sin_perdida_todo_llega_y_converge(results) -> None:
    send = next(r for r in results.send if r.channel.label == "0 %")
    assert send.measured_loss == 0 and send.delivered == send.confirmed == send.reports
    repl = next(r for r in results.replication if r.channel.label == "0 %")
    assert repl.converge_s is not None and repl.invalid == 0 and repl.takeovers == 0


def test_lo_critico_sale_primero_y_la_espera_de_los_bajos_es_acotada(results) -> None:
    for r in results.send:
        critical, low = r.latencies[Urgency.CRITICA], r.latencies[Urgency.BAJA]
        assert critical and low
        assert percentile(critical, 0.5) < percentile(low, 0.5)
        assert r.missing[Urgency.BAJA] == 0 and max(low) <= r.horizon_s
        assert sum(map(len, r.latencies.values())) + sum(r.missing.values()) == r.reports


def test_con_60_por_ciento_la_perdida_medida_coincide(results) -> None:
    for r in results.send + results.replication:
        if r.channel.label == "60 %":
            assert r.measured_loss == pytest.approx(0.6, abs=0.1)


def test_particion_y_caida_terminan_con_hashes_iguales(results) -> None:
    for p in results.partition:
        assert p.converge_s is not None and p.parallel == 0
    for run_ in results.failover:
        r = run_.result
        assert r.taker == "B" and r.pending_before == 2 and r.pending_after == 0
        assert r.converge_s is not None


# --- La tabla -------------------------------------------------------------------------------


def test_tabla_con_todas_las_metricas_de_seccion_15(table) -> None:
    for title in (
        "Bytes por reporte", "Ahorro del diccionario", "Tasa de entrega", "Latencia p50 / p95",
        "Tiempo por reporte", "Sensibilidad y precisión", "Duplicados bien vinculados",
        "llegan al LLM", "Casos ambiguos", "RAG", "Tiempo de recuperación", "Hashes iguales",
        "cambio de nodo del tablero", "miniatura",
    ):  # fmt: skip
        assert title in table, title
    for section in ("## Envío", "## Replicación", "## Partición", "## Caída de A"):
        assert section in table
    assert "SOS escombros 21 B" in table
    assert "| critica |" in table  # matriz de confusión


def test_lo_que_no_se_puede_medir_sale_como_pendiente(table) -> None:
    rows = {line.split(" | ")[0]: line for line in table.splitlines() if line.startswith("| ")}
    for metric, task in (
        ("| Duplicados bien vinculados, fusiones erróneas, rumores", "pendiente (D4)"),
        ("| Calidad de la búsqueda del RAG", "pendiente (D8)"),
        ("| Tamaño de la miniatura de foto", "pendiente (nivel 3)"),
        ("| Casos ambiguos resueltos con LLM en la nube y local", "pendiente: requiere LLM"),
        ("| Tiempo de cambio de nodo del tablero", "pendiente (se mide en el navegador"),
    ):
        value = rows[metric].split(" | ")[1]
        assert value.startswith(task), value
    # El objetivo de la miniatura queda en su columna, no como valor medido
    assert rows["| Tamaño de la miniatura de foto"].split(" | ")[1] != "< 3 KB"


def test_comando_escribe_la_tabla_y_el_progreso_va_aparte(tmp_path, capsys) -> None:
    out = tmp_path / "resultados.md"
    main(["--rapido", "--semilla", "7", "--salida", str(out)])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[2/2] lora 60 %" in captured.err and "listo en" in captured.err
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Banco de fallos · semilla 7")
    assert "python -m guadua.bench --semilla 7 --rapido" in text
