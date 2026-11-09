"""Tablas en Markdown del banco de fallos, listas para pegar en el documento de resultados.

Las métricas que todavía no se pueden medir salen como "pendiente (tarea)"; ningún objetivo
se muestra como si fuera un valor medido.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from guadua.bench.escenarios import (
    LEVELS,
    FailoverRun,
    PartitionResult,
    ReplicationResult,
    SendResult,
    percentile,
)
from guadua.classifier.evaluate import Report
from guadua.classifier.urgency import Urgency

NAMES = {lvl: lvl.name.lower() for lvl in Urgency}
CPU_NOTE = "tiempo de máquina, varía entre corridas"


@dataclass(slots=True)
class Static:
    """Métricas que no dependen del canal."""

    sos_bytes: int
    report_count: int  # reportes de prueba que caben (texto ≤ 100 B)
    too_long: int  # reportes de prueba cuyo texto no cabe en 100 B
    mean_bytes: float
    max_bytes: int
    under_sms: float  # fracción de mensajes que caben en un SMS (140 B)
    dictionary_savings: float
    evaluation: Report


@dataclass(slots=True)
class Results:
    seed: int
    command: str
    static: Static
    send: list[SendResult]
    replication: list[ReplicationResult]
    partition: list[PartitionResult]
    failover: list[FailoverRun]


# --- Formato ------------------------------------------------------------------------------


def pct(value: float, decimals: int = 0) -> str:
    return f"{value * 100:.{decimals}f} %"


def secs(value: float | None, missing: str = "no llegó") -> str:
    return missing if value is None else f"{value:.0f} s"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], right_from: int = 1) -> str:
    align = [":--" if i < right_from else "--:" for i in range(len(header))]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(align) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def _latency(result: SendResult, level: Urgency) -> str:
    values = result.latencies[level]
    if not values:
        return "—" if not result.missing[level] else "no llegó"
    p50, p95 = percentile(values, 0.5), percentile(values, 0.95)
    text = f"{p50:.0f} / {p95:.0f} s"
    return text + (f" (+{result.missing[level]} sin llegar)" if result.missing[level] else "")


def max_low_wait(result: SendResult) -> str:
    if result.missing[Urgency.BAJA]:
        return f"> {result.horizon_s:.0f} s ({result.missing[Urgency.BAJA]} sin llegar)"
    values = result.latencies[Urgency.BAJA]
    return f"{max(values):.0f} s" if values else "—"


def _pick[T](rows: Sequence[T], profile: str, label: str) -> T | None:
    return next((r for r in rows if r.channel.name == profile and r.channel.label == label), None)


# --- Secciones ----------------------------------------------------------------------------


def summary(res: Results) -> str:
    st, ev = res.static, res.static.evaluation
    lora = {label: _pick(res.send, "lora", label) for label in ("0 %", "30 %", "60 %", "cortes")}
    delivery = " · ".join(
        f"LoRa {label}: {pct(r.delivery_rate)}"
        for label, r in lora.items()
        if r is not None and label != "0 %"
    )
    ref = lora["30 %"] or (res.send[0] if res.send else None)
    latency = (
        f"{ref.channel.name} {ref.channel.label}: "
        + " · ".join(f"{NAMES[lvl]} {_latency(ref, lvl)}" for lvl in LEVELS)
        + f"; espera máx. de los bajos {max_low_wait(ref)}"
        if ref
        else "—"
    )
    same_hash = [r.converge_s is not None for r in res.replication + res.partition] + [
        r.result.converge_s is not None for r in res.failover
    ]
    fail = _pick(res.failover, "lora", "30 %") or (res.failover[0] if res.failover else None)
    recovery = (
        f"{fail.channel.name} {fail.channel.label}: vuelve al día en "
        f"{secs(fail.result.recovery_s, 'siguió solo')}, converge en "
        f"{secs(fail.result.converge_s, 'no convergió')}"
        if fail
        else "—"
    )
    rows = [
        (
            "Bytes por reporte",
            f"SOS escombros {st.sos_bytes} B; con texto, promedio {st.mean_bytes:.1f} B y "
            f"máximo {st.max_bytes} B; {pct(st.under_sms, 1)} caben en 140 B",
            "21 B y promedio < 140 B",
        ),
        (
            "Ahorro del diccionario frente a no comprimir",
            pct(st.dictionary_savings, 1),
            "—",
        ),
        ("Tasa de entrega (emisor → nodo, 30 min)", delivery or "—", "—"),
        ("Latencia p50 / p95 por urgencia", latency, "lo crítico primero; espera acotada"),
        (
            "Tiempo por reporte: reglas / clasificador (mediana)",
            f"{ev.rules_ms[0]:.3f} ms / {ev.classifier_ms[0]:.3f} ms ({CPU_NOTE})",
            "< 1 ms / < 2 ms",
        ),
        (
            "Sensibilidad y precisión en críticos (reglas + clasificador)",
            f"{pct(ev.system.sensitivity, 1)} / {pct(ev.system.precision, 1)} "
            f"en {ev.test_size} reportes de prueba; matriz abajo",
            "—",
        ),
        ("Duplicados bien vinculados, fusiones erróneas, rumores", "pendiente (D4)", "—"),
        ("Reportes que llegan al LLM (zona gris)", pct(ev.ambiguous_rate, 1), "cerca de 15 %"),
        (
            "Casos ambiguos resueltos con LLM en la nube y local",
            "pendiente: requiere LLM (`python -m guadua.agents.triaje_eval`)",
            "—",
        ),
        ("Calidad de la búsqueda del RAG", "pendiente (D8)", "—"),
        ("Tiempo de recuperación de un nodo", recovery, "—"),
        (
            "Hashes iguales tras sincronizar",
            f"{sum(same_hash)} de {len(same_hash)} escenarios",
            "todos",
        ),
        (
            "Tiempo de cambio de nodo del tablero",
            "pendiente (se mide en el navegador; N14 / D9)",
            "—",
        ),
        ("Tamaño de la miniatura de foto", "pendiente (nivel 3)", "< 3 KB"),
    ]
    return _table(("Métrica", "Valor medido", "Objetivo"), rows, right_from=3)


def matrix(report: Report) -> str:
    header = ["real \\ publicado", *(NAMES[p] for p in LEVELS)]
    rows = [[NAMES[t], *(str(report.matrix[t][p]) for p in LEVELS)] for t in LEVELS]
    return _table(header, rows)


def send_table(results: Sequence[SendResult]) -> str:
    rows = [
        [
            r.channel.name,
            r.channel.label,
            pct(r.measured_loss),
            f"{r.delivered}/{r.reports} ({pct(r.delivery_rate)})",
            f"{r.frames / r.reports:.1f}",
            *(_latency(r, lvl) for lvl in LEVELS),
            max_low_wait(r),
        ]
        for r in results
    ]
    header = ("Perfil", "Pérdida", "Medida", "Entregados", "Tramas/reporte",
              *(f"{NAMES[lvl]} p50 / p95" for lvl in LEVELS), "Espera máx. baja")  # fmt: skip
    return _table(header, rows, right_from=2)


def replication_table(results: Sequence[ReplicationResult]) -> str:
    rows = [
        [
            r.channel.name,
            r.channel.label,
            pct(r.measured_loss),
            secs(r.complete_s),
            secs(r.converge_s, "no convergió"),
            str(r.rounds),
            str(r.frames),
            str(r.duplicates),
            str(r.invalid),
            str(r.takeovers),
        ]
        for r in results
    ]
    header = ("Perfil", "Pérdida", "Medida", "C tiene todo", "Hashes iguales", "Rondas",
              "Tramas", "Duplicados", "Inválidos", "Tomas")  # fmt: skip
    return _table(header, rows, right_from=2)


def partition_table(results: Sequence[PartitionResult]) -> str:
    rows = [
        [
            r.channel.name,
            r.channel.label,
            secs(r.converge_s, "no convergió"),
            str(r.rounds),
            str(r.frames),
            str(r.takeovers),
            str(r.parallel),
        ]
        for r in results
    ]
    header = ("Perfil", "Pérdida", "Hashes iguales tras unir", "Rondas", "Tramas",
              "Tomas", "Pasos en paralelo")  # fmt: skip
    return _table(header, rows, right_from=2)


def failover_table(runs: Sequence[FailoverRun]) -> str:
    rows = []
    for run in runs:
        r = run.result
        rows.append(
            [
                run.channel.name,
                run.channel.label,
                r.taker or "nadie",
                secs(r.detection_s, "no detectó"),
                secs(r.takeover_s, "no tomó"),
                f"{r.pending_before} → {r.pending_after}",
                secs(r.recovery_s, "siguió solo"),
                secs(r.converge_s, "no convergió"),
            ]
        )
    header = ("Perfil", "Pérdida", "Toma", "Detección", "Toma completa", "Pendientes",
              "Recuperación de A", "Hashes iguales")  # fmt: skip
    return _table(header, rows, right_from=3)


def render(res: Results) -> str:
    first = res.send[0].reports if res.send else 0
    repl = res.replication[0].reports if res.replication else 0
    side = res.partition[0].reports_per_side if res.partition else 0
    split = res.partition[0].split_s if res.partition else 0
    parts = [
        f"# Banco de fallos · semilla {res.seed}",
        "",
        f"Comando: `{res.command}`. Canal simulado con reloj simulado (sin red real, Docker, "
        "Internet ni LLM); una ronda de gossip y resiliencia = 2 s. Con la misma semilla sale "
        f"la misma tabla, salvo los tiempos marcados como «{CPU_NOTE}». Pérdida «0/30/60 %» = "
        "pérdida suelta sin cortes; «cortes» = el perfil tal cual (§5.3). Los retrasos del "
        "perfil cuentan en todas las filas, así que también hay desorden.",
        "",
        "## Métricas de §15",
        "",
        summary(res),
        "",
        "Matriz de confusión (reglas + clasificador, conjunto de prueba):",
        "",
        matrix(res.static.evaluation),
        "",
        f"## Envío: ráfaga de {first} reportes por la cola (N11), emisor → nodo A",
        "",
        "Todos entran a la cola en t = 0 con la urgencia de N7; el emisor transmite una vez "
        "por segundo y el nodo responde con ACK. Latencia = hasta que el reporte llega al nodo. "
        "Horizonte: 30 min.",
        "",
        send_table(res.send),
        "",
        f"## Replicación A → B → C (gossip, N5): {repl} reportes que entran por A",
        "",
        "«Tomas» = reasignaciones publicadas: un vecino vivo que se dio por caído.",
        "",
        replication_table(res.replication),
        "",
        f"## Partición B | C de {split:.0f} s con {side} reportes por lado, y reconexión",
        "",
        "«Pasos en paralelo» = normalizado o urgencia de un mismo reporte hechos por dos nodos.",
        "",
        partition_table(res.partition),
        "",
        "## Caída de A con 2 reportes sin procesar, toma y vuelta (N12)",
        "",
        "Detección y toma cuentan desde la caída; con mucha pérdida B puede llevar segundos sin "
        "oír a A desde antes, y la detección sale menor que el plazo de 10 s. «Recuperación» "
        "= del regreso de A a ponerse al día con un vecino; «siguió solo» = no oyó a ningún "
        "vecino en 10 s y empezó a actuar sin esperar.",
        "",
        failover_table(res.failover),
        "",
        "Limitación: datos sintéticos (data/sinteticos/reportes.csv) y perfiles simulados; "
        "no son mediciones de equipos reales.",
    ]
    if res.static.too_long:
        parts.insert(
            -1,
            f"{res.static.too_long} reportes de prueba no caben en 100 B de texto y no se "
            "cuentan en los bytes por reporte.\n",
        )
    return "\n".join(parts) + "\n"
