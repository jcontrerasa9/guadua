# Agente 7 · Vigía

Código: `src/guadua/agents/vigia.py` (D6). Diseño: `docs/diseno.md` §10 y §3.2. Plan: `docs/plan.md`.

| Campo | Agente 7 · Vigía |
| :---- | :---- |
| **Objetivo** | Que ningún caso de emergencia abierto quede olvidado ni rezagado en el tiempo, solicitando reevaluación cuando surge evidencia nueva o alertando demoras críticas en la atención |
| **Entradas** | Registro de eventos del nodo (`EventStore`); lista de incidentes abiertos sin atender ordenados por antigüedad; eventos de nueva evidencia (reportes agrupados, transcripciones de radio, confirmaciones de campo) |
| **Salidas** | Solicitudes tipadas (`Solicitud`) dirigidas a triaje, a coordinación o alertas al tablero de comando; registro explicativo en los pasos del incidente |
| **Herramientas (lista blanca)** | `incidentes_sin_atender`, `evidencia_desde`, `solicitar_reevaluacion`, `alertar_tablero` |
| **Memoria** | Corto plazo: estado de la ronda de inspección y registro de solicitudes recientes. Largo plazo: ninguna propia; el estado reside en los eventos inmutables del registro |
| **Límites de autonomía** | **NUNCA cambia prioridades:** no modifica la urgencia (`level`) ni el piso (`floor`) de ningún incidente o reporte. No asigna recursos ni despacha unidades directamente. Solo emite solicitudes justificadas y alertas para que los agentes respectivos o el operador humano actúen |
| **Manejo de errores** | Si el LLM no está disponible (`NullLLM`, sin conexión o excepción), el vigía opera al 100% de manera determinista mediante reglas y plantillas predefinidas. Una falla en el vigía nunca interrumpe el nodo, el canal ni la recepción de reportes |
| **Criterios de terminación** | Ronda de escaneo finalizada con la lista de solicitudes y alertas generadas para los casos desatendidos |

---

## 1. Cuándo actúa

El agente vigía inspecciona periódicamente o por iniciativa propia el estado de los incidentes en el almacén de eventos:

1. **Casos sin ficha (`sin_ficha`):** Incidentes que tienen reportes y urgencia estimada, pero aún no cuentan con una ficha de coordinación formulada tras un período de espera ($T \ge T_{\text{olvido}}$).
2. **Casos con ficha pendiente de decisión (`pendiente`):** Incidentes cuya ficha de despacho no ha sido aprobada, editada ni rechazada por el operador humano tras una ventana de tiempo considerable.
3. **Casos con ambigüedad o verificación pendiente:** Incidentes marcados como `verify_now=True` o `ambiguous=True` que no han recibido confirmación en campo ni resolución.
4. **Casos con evidencia nueva:** Incidentes abiertos que recibieron reportes adicionales, nuevos datos de víctimas/atrapados, riesgos en escena o transcripciones de radio después de la última estimación de urgencia o ficha.

---

## 2. Invariante Inviolable: Nunca cambia prioridades

De acuerdo con la **Regla 1 de `AGENTS.md`** y el diseño del sistema SARD Guadua:
- El Vigía **NO tiene permitido** alterar el nivel de urgencia (`level`) ni el piso determinista (`floor`) de un reporte o incidente.
- Si un incidente crítico o de alto riesgo está desatendido, el vigía solicita a `coordinacion` que agilice la ficha, solicita a `triaje` que reevalúe ante nuevas pruebas, o emite una alerta de atención inmediata (`alertar_tablero`), pero la urgencia permanece intacta bajo las reglas del clasificador y triaje.

---

## 3. Tipos de Solicitudes Emitidas

| Destinatario | Acción solicitada | Cuándo se genera |
| :--- | :--- | :--- |
| **Triaje** (`target="triaje"`) | `reevaluacion_triaje` | Se agruparon nuevos reportes al incidente tras el triaje previo, o el caso quedó en zona gris (`ambiguous`) sin resolver tras el tiempo límite. |
| **Coordinación** (`target="coordinacion"`) | `crear_ficha` / `revisar_demora` | El incidente carece de ficha de despacho, o la ficha existente lleva tiempo excesivo sin respuesta humana mientras se acumulan reportes. |
| **Tablero** (`target="tablero"`) | `alerta_olvido` | Incidente de prioridad Crítica o Alta que supera el umbral máximo de desatención sin despacho aprobado. |

---

## 4. Operación con y sin LLM

- **Con LLM:** Utiliza el modelo configurado (Gemini en la nube u Ollama en local) para sintetizar la cronología de eventos y redactar un motivo explicativo claro en lenguaje natural para el despachador.
- **Sin LLM (`NullLLM`):** Construye el motivo de la solicitud usando una plantilla determinista basada en el tipo de siniestro, tiempo transcurrido y cantidad de reportes nuevos. Nunca falla ni se bloquea.
