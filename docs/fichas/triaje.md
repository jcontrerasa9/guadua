# Agente 2 · Triaje

Código: `src/guadua/agents/triaje.py` (N8). Evaluación: `python -m guadua.agents.triaje_eval`. Diseño: §10 y §11.

| Campo | Agente 2 · Triaje |
| :---- | :---- |
| Objetivo | Que cada reporte dudoso tenga una urgencia justificada y que ningún caso posiblemente crítico quede mal clasificado sin revisión |
| Entradas | Una `urgencia_estimada` de N7 (reglas + clasificador) con `ambiguous=True`, del propio nodo; el `reporte_normalizado` al que apunta; una copia del registro para las herramientas |
| Salidas | Una `urgencia_estimada` nueva con `source="agente"`, `refs` a la de N7, el mismo piso, `verify_now` si lo pidió y sus pasos en `steps`. Resuelta (`ambiguous=False`) o "ambiguo, revisar" (`ambiguous=True`, nivel de N7) |
| Herramientas | `incidentes_cercanos` (mismo barrio o geohash de 6 caracteres, ventana de 10 a 180 min), `radio_en_zona` (D7: lo que dijo la radio en el mismo lugar y ventana, con si lo vieron en el sitio; no cuenta los reportes de radio sin lugar ni los de una transcripción corregida), `marcar_verificar_ya` (con motivo), `fijar_urgencia` (termina). Nada más: otra herramienta manda el caso a revisión |
| Memoria | Corto plazo: los mensajes y pasos del caso en curso. Largo plazo: ninguna; el estado vive en el registro de eventos |
| Límites | No diagnostica. Sube sin límite. Baja solo un nivel desde el de N7: si propone más, se baja uno y queda escrito. Con una cita textual del reporte (se compara sin tildes ni mayúsculas; si no está, se le devuelve el error una sola vez para que la corrija) y nunca bajo el piso de las reglas (`apply_floor`). Máximo 6 llamadas al LLM y 180 s por caso. La decisión final es de una persona |
| Errores | Sin LLM (`NullLLM`, ninguno configurado en la cadena), respuesta que no es JSON del protocolo, herramienta no permitida, argumentos inválidos, propuesta que viola la regla 1, pasos o tiempo agotados, o una excepción: el caso queda "ambiguo, revisar" con el nivel de N7 y el motivo en el último paso. Nunca tumba el nodo, el ACK ni la correlación |
| Terminación | Urgencia publicada (resuelta o a revisión humana) |

## Cuándo actúa

Solo con lo que N7 marca `ambiguous`: ninguna regla activada y el clasificador duda, texto con muchas palabras desconocidas, o texto con señales que el clasificador no entiende (`classifier/cues.py`: `SOLO_ANIMALES`, `DUDA`; se le pasan al LLM en `senales_de_lenguaje`). En el conjunto `test` es el 18,2 % de los reportes. Nunca actúa sobre su propia urgencia (no hay bucle) ni en un nodo que no es el dueño del reporte.

**Limitación conocida:** el clasificador no entiende significado; combinaciones nuevas de palabras conocidas ("no encontramos a mi mamá, estaba adentro") salen con confianza y no llegan al agente. En el conjunto `ambiguo`, 176 de 318 casos salen mal sin quedar marcados. Ver "Pendiente de N8" en `docs/plan.md`.

## LLM

Cadena `Gemini (nube) → Ollama (local) → ninguno`, armada con las variables de `.env.example` leídas del entorno (`providers/fallback.py`). Un proveedor que falla se salta 60 s. Los dos clientes usan el mismo protocolo JSON (`providers/json_protocol.py`) en lugar del *function calling* de cada uno, porque los modelos pequeños lo usan mal.

En el nodo, el ciclo corre en otro hilo: el ACK y la correlación no lo esperan. El resultado se guarda de vuelta en el hilo del nodo.

## Registro

- **En el evento** (inmutable y replicado): cada llamada al LLM con proveedor/modelo, tiempo y lo que pidió o propuso; el resultado resumido de cada herramienta; y la decisión final (`decisión: bajada por el agente: alta → media; motivo: …; cita: «…»`) o `ambiguo, revisar: <por qué>`. Se ve en el desplegable "por qué está aquí" del tablero.
- **En el log** del proceso: los mismos pasos en el logger `guadua.triaje` (`docker compose logs node-a`), y las fallas de proveedores en `guadua.llm`.

## Tarjeta

Vale la urgencia más reciente de cada reporte: la del agente reemplaza a la de N7 porque la referencia. Si tras una partición quedan dos conclusiones paralelas, se conserva la más alta (diseño §9). Nunca queda bajo el piso.

## Pendiente

Ver "Pendiente de N8" en `docs/plan.md`: `respuesta_seguimiento` (D5a), `solicitar_verificacion_campo` (tipo de evento nuevo) y disparador por palabras de duda. Retomar casos tras una caída quedó en N12 (`docs/fichas/resiliencia.md`).
