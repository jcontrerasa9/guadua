"""Cálculo de confianza, factores de independencia, detección de rumores y contradicciones.

Implementa el modelo probabilístico de independencia de fuentes de docs/diseno.md §12:
    Confianza = 1 - Π (1 - p_i * ind_i)
con tope acordado para reportes de segunda mano.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from guadua.protocol.header import Incident
from guadua.store.event_types import ConfirmacionCampo, ReporteNormalizado

# --- Pesos base de fuentes (p) - docs/diseno.md §12 ------------------------------------

PESO_CONFIRMACION_CAMPO: float = 1.00  # Verificado por rescatista en sitio
PESO_RESCATISTA_RADIO_DIRECTO: float = 0.85  # Rescatista por VHF, observación directa
PESO_CIUDADANO_DIRECTO: float = 0.60  # Ciudadano "sí, lo veo"
PESO_SEGUNDA_MANO: float = 0.10  # Ciudadano o radio "me contaron"

# --- Factores de independencia (ind) ---------------------------------------------------

IND_MISMO_DISPOSITIVO: float = 0.0  # Mismo device_id (primeros 8 dígitos hex)
IND_TEXTO_CASI_IDENTICO: float = 0.05  # Similitud de texto >= 0.85 (copiado/reenviado)
IND_TEXTO_SIMILAR: float = 0.30  # Similitud de texto entre 0.50 y 0.85
IND_UBICACION_DUDOSA: float = 0.50  # Ubicación aproximada o limítrofe
IND_TOTALMENTE_INDEPENDIENTE: float = 1.0  # Distinto dispositivo, redacción propia, directo

# --- Umbrales de confianza, rumor y tope de segunda mano ------------------------------

UMBRAL_CONFIANZA_BAJA: float = 0.30  # < 0.30 es confianza baja
UMBRAL_CONFIANZA_ALTA: float = 0.70  # >= 0.70 es confianza alta
TOPE_CONFIANZA_SEGUNDA_MANO: float = 0.25  # Aporte máximo acumulado de fuentes de segunda mano

SIMILITUD_CASI_IDENTICA: float = 0.85
SIMILITUD_SIMILAR: float = 0.50

# Frases para detección de contradicciones en texto
_TERMINOS_ATRAPADOS: tuple[str, ...] = (
    "atrapado",
    "atrapada",
    "atrapados",
    "atrapadas",
    "sepultado",
    "sepultada",
    "sepultados",
    "sepultadas",
)

_TERMINOS_LIBERADOS: tuple[str, ...] = (
    "ya los sacaron",
    "ya las sacaron",
    "ya lo sacaron",
    "ya la sacaron",
    "ya salieron",
    "rescatados",
    "rescatadas",
    "rescatado",
    "rescatada",
    "no hay atrapados",
    "falsa alarma",
    "descartado",
)

_TERMINOS_FUEGO_ACTIVO: tuple[str, ...] = (
    "fuego",
    "incendio",
    "llamas",
)

_TERMINOS_FUEGO_CONTROLADO: tuple[str, ...] = (
    "fuego apagado",
    "incendio controlado",
    "ya lo apagaron",
    "ya se apago",
    "ya se apagó",
    "controlado",
)


def extraer_device_id(report_id: str) -> str:
    """Extrae los primeros 8 dígitos hex del dispositivo a partir del report_id."""
    return report_id.split("-", 1)[0].lower()


def calcular_similitud_texto(text1: str | None, text2: str | None) -> float | None:
    """Calcula similitud de texto normalizado usando SequenceMatcher."""
    if not text1 or not text2:
        return None
    s1 = " ".join(text1.lower().split())
    s2 = " ".join(text2.lower().split())
    if not s1 or not s2:
        return None
    return SequenceMatcher(None, s1, s2).ratio()


def calcular_independencia(
    reporte: ReporteNormalizado,
    reportes_previos: list[ReporteNormalizado],
    *,
    ubicacion_dudosa: bool = False,
) -> float:
    """Calcula el factor de independencia ind_i del reporte respecto a reportes previos."""
    dev_id = extraer_device_id(reporte.report_id)
    prev_dev_ids = {extraer_device_id(r.report_id) for r in reportes_previos}
    if dev_id in prev_dev_ids:
        return IND_MISMO_DISPOSITIVO

    max_sim: float | None = None
    if reporte.text:
        sims = [calcular_similitud_texto(reporte.text, r.text) for r in reportes_previos if r.text]
        valid_sims = [s for s in sims if s is not None]
        if valid_sims:
            max_sim = max(valid_sims)

    ind = IND_TOTALMENTE_INDEPENDIENTE
    if max_sim is not None:
        if max_sim >= SIMILITUD_CASI_IDENTICA:
            return IND_TEXTO_CASI_IDENTICO
        elif max_sim >= SIMILITUD_SIMILAR:
            ind = min(ind, IND_TEXTO_SIMILAR)

    if ubicacion_dudosa:
        ind = min(ind, IND_UBICACION_DUDOSA)

    return ind


def obtener_peso_fuente(
    reporte: ReporteNormalizado,
    channel: str | None = None,
) -> float:
    """Determina el peso base p según el origen (radio, ciudadano, directo vs segunda mano)."""
    if channel in ("radio", "vhf"):
        return PESO_RESCATISTA_RADIO_DIRECTO if reporte.direct_observation else PESO_SEGUNDA_MANO
    return PESO_CIUDADANO_DIRECTO if reporte.direct_observation else PESO_SEGUNDA_MANO


def calcular_confianza_y_rumor(
    reportes: list[ReporteNormalizado],
    *,
    channels: dict[str, str] | None = None,
    confirmaciones: list[ConfirmacionCampo] | None = None,
    ubicacion_dudosa: bool = False,
) -> tuple[float, bool]:
    """Calcula la confianza en [0.0, 1.0] y si se clasifica como rumor.

    Retorna:
        (confianza, rumor)
    """
    if confirmaciones and any(c.confirmed for c in confirmaciones):
        # Confirmación positiva por rescatista en campo -> Verificado (1.00)
        return 1.00, False

    if not reportes:
        return 0.0, False

    prod_directos = 1.0
    prod_segunda_mano = 1.0
    count_directos = 0
    count_segunda_mano = 0

    for i, rep in enumerate(reportes):
        previos = reportes[:i]
        ind = calcular_independencia(rep, previos, ubicacion_dudosa=ubicacion_dudosa)
        ch = (channels or {}).get(rep.report_id)
        p = obtener_peso_fuente(rep, ch)

        term = 1.0 - (p * ind)
        if not rep.direct_observation:
            prod_segunda_mano *= term
            count_segunda_mano += 1
        else:
            prod_directos *= term
            count_directos += 1

    # Aplicar tope a la contribución acumulada de segunda mano
    # Π_sm_capped >= 1.0 - TOPE_CONFIANZA_SEGUNDA_MANO
    min_prod_sm = max(0.0, 1.0 - TOPE_CONFIANZA_SEGUNDA_MANO)
    prod_sm_capped = max(min_prod_sm, prod_segunda_mano)

    prod_total = prod_directos * prod_sm_capped
    confianza = round(min(1.0, max(0.0, 1.0 - prod_total)), 4)

    total_fuentes = count_directos + count_segunda_mano
    mayoria_segunda_mano = (count_segunda_mano > total_fuentes / 2) if total_fuentes > 0 else False
    rumor = (confianza < UMBRAL_CONFIANZA_BAJA) and mayoria_segunda_mano

    return confianza, rumor


def detectar_contradicciones(
    reportes: list[tuple[ReporteNormalizado, float]],  # (reporte, received_at)
    confirmaciones: list[tuple[ConfirmacionCampo, str, float]] | None = None,  # (conf, origin, t)
) -> tuple[str, ...]:
    """Detecta inconsistencias o contradicciones entre reportes o con confirmaciones de campo.

    Cada contradicción incluye su origen y marca temporal (timestamp).
    """
    contradictions: list[str] = []

    # 1. Confirmaciones de campo negativas
    if confirmaciones:
        for conf, origin_id, t_conf in confirmaciones:
            if not conf.confirmed:
                nota = f": {conf.note}" if conf.note else ""
                contradictions.append(
                    f"[confirmacion_campo] origen={origin_id} t={t_conf:.1f}: "
                    f"Incidente descartado en campo por rescatista{nota}"
                )

    # 2. Inconsistencias entre reportes del mismo grupo
    if len(reportes) >= 2:
        # Ordenar cronológicamente por received_at
        ordenados = sorted(reportes, key=lambda x: x[1])

        # Verificar atrapados vs liberados/falsa alarma
        hay_atrapados_previo: bool = False
        rep_atrapados: ReporteNormalizado | None = None
        t_atrapados: float | None = None

        for rep, t_rep in ordenados:
            text_lower = (rep.text or "").lower()
            indica_atrapados = (
                rep.incident == Incident.ATRAPADOS
                or "atrapados" in rep.vital_signs
                or any(w in text_lower for w in _TERMINOS_ATRAPADOS)
            )
            indica_liberados = any(w in text_lower for w in _TERMINOS_LIBERADOS)

            if indica_atrapados and not hay_atrapados_previo:
                hay_atrapados_previo = True
                rep_atrapados = rep
                t_atrapados = t_rep

            if indica_liberados and hay_atrapados_previo and rep_atrapados is not None:
                contradictions.append(
                    f"[reporte] origen={rep.report_id} t={t_rep:.1f}: "
                    f"Señala rescate o falsa alarma en contradicción con "
                    f"reporte {rep_atrapados.report_id} t={t_atrapados:.1f} ('atrapados')"
                )

    return tuple(contradictions)
