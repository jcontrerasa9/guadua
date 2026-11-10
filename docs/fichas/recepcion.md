# Agente 1 · Recepción (preguntas de seguimiento)

Código: `src/guadua/agents/recepcion.py` (D5a). Contrato: `pregunta_seguimiento` y `respuesta_seguimiento` en `docs/contratos.md`. Diseño: §3.1, §6.4 y §10.

Hoy el agente hace la parte de las **preguntas de seguimiento**. La normalización sigue con "reglas simples" en `node/pipeline.py`; separar transmisiones de radio e interpretar referencias llegan con D7 y tareas posteriores.

| Campo | Agente 1 · Recepción |
| :---- | :---- |
| Objetivo | Que a un reporte al que le falta un dato clave para el rescate se le pueda completar con un toque del ciudadano, sin inventar nada |
| Entradas | Un `reporte_normalizado` del propio nodo (o de uno que tomó, N12) y el `reporte` al que apunta |
| Salidas | Como máximo una `pregunta_seguimiento` por reporte, con `refs` al `reporte_normalizado`: clave del catálogo o `libre`, tipo, texto, opciones, motivo y quién la decidió (`reglas` o `agente` con su modelo) |
| Herramientas | `enviar_pregunta_seguimiento` es el propio evento. `marcar_campo_desconocido` sigue siendo `unknown_fields` de la normalización. El LLM no usa herramientas: responde un solo JSON |
| Memoria | Ninguna propia; el estado vive en el registro de eventos |
| Límites | Una pregunta por reporte. No pregunta en un SOS de escombros, con batería crítica ni por radio VHF. No pregunta lo que la cabecera o el texto ya dicen ni lo que no aplica (un animal, alguien que ya murió). Si falta saber si alguien no respira o sangra mucho, se pregunta eso: el LLM puede reformularlo, no cambiarlo. Con datos faltantes, el LLM no puede decidir no preguntar. Una respuesta nunca baja la urgencia |
| Errores | Sin LLM, si la cadena entera falla, si responde algo fuera del protocolo, elige una pregunta que no es candidata o redacta una que no pasa el validador: se usa la primera candidata de las reglas, con el motivo del descarte. Nunca tumba el nodo ni el ACK |
| Terminación | Pregunta publicada, o ninguna si no hace falta |

## Qué pregunta

Las reglas listan los **datos que faltan**, del más importante al menos: primero lo que puede subir el piso (regla 1), luego el riesgo para los rescatistas.

| Clave | Pregunta base (Sí / No) | Con opciones | Cuándo falta |
| :-- | :-- | :-- | :-- |
| `no_respira_o_sangrado` | ¿Alguien no respira o sangra mucho? | — | Se sabe que hay personas afectadas (atrapados o herido) y ni la bandera ni el texto lo dicen. No aplica a un animal ni a alguien que ya murió |
| `atrapados` | ¿Hay personas atrapadas? | — | Colapso o incendio, sin bandera ni texto de atrapados. No aplica a un animal |
| `gas_o_cables` | ¿Huele a gas o hay cables caídos? | — | Colapso, incendio o atrapados, sin bandera 3 ni señal de gas o cables en el texto (D3a) |
| `personas` | ¿Son más de 5 personas? | ¿Cuántas personas hay? 1 · 2 a 5 · 6 a 20 · Más de 20 | Personas "no sé" en atrapados, colapso, herido o incendio. La PWA aún no tiene ese campo, así que casi siempre falta: por eso va de última y no se pregunta en una fuga de gas ni en una vía bloqueada |

"No sé" siempre se puede responder.

## LLM

La misma cadena que el triaje: Gemini (con Internet) → Ollama (sin Internet) → reglas (`providers/fallback.py`, variables de `.env.example`). Se consulta si falta algún dato o si hay texto que leer. El prompt le pide preguntar lo que cambia cómo atienden los rescatistas (si hay personas en peligro, dónde están, cuántas, por dónde se entra, qué peligros hay) **con las palabras del reporte**. Puede:

- **Preguntar por un dato que falta, reformulado** con las palabras del reporte: «¿Hay personas atrapadas en la casa que se quema?». Sigue siendo de Sí / No y sobre el mismo dato, para que la clave diga qué significa la respuesta. `personas` no se reformula: su respuesta se traduce a los rangos de la cabecera.
- **Preguntar otra cosa** (`key: "libre"`), de un tipo que la PWA sabe mostrar, si el reporte muestra que importa más.
- **No preguntar**, solo si no falta ningún dato.

Si falta lo vital, la pregunta debe ser sobre eso; si propone otra, se usa la de las reglas. Toda pregunta del LLM pasa un validador fijo: una sola pregunta entre ¿ y ?, máximo 90 caracteres, sin pedir nombres, documentos, teléfonos, dinero ni enlaces; de 2 a 4 opciones cortas sin repetir. Texto libre nunca por SMS.

**Cuota de Gemini:** la capa gratuita responde `429 Too Many Requests` tras unas 15 llamadas seguidas, y el triaje y la recepción comparten la cadena. Al fallar, Gemini se salta 60 s; sin `OLLAMA_MODEL`, mientras tanto deciden las reglas.

## Evaluación

```bash
python -m guadua.agents.recepcion_eval            # reglas (también corre en pytest)
docker exec guadua-node-a-1 python -m guadua.agents.recepcion_eval --llm
```

20 casos como los que manda la PWA, varios de pruebas reales ("Creo que hay un gato herido", "Tiene un brazo doblado raro", "Hay un muerto"). Cada uno dice qué datos son pertinentes de preguntar; con LLM imprime además el texto de cada pregunta para revisarlo a ojo. Primera corrida con `gemini-3.1-flash-lite` (9 de noviembre de 2026): 20/20 pertinentes, 13 preguntas reformuladas con las palabras del reporte, ~1 s por pregunta. Las 4 últimas llamadas recibieron 429 y quedaron en reglas.

El texto del reporte llega al modelo como dato; el mensaje de sistema le pide ignorar órdenes que vengan en él, y el validador acota lo que pueda salir.

## Tipos de pregunta y la PWA

`SARD_QUESTION_KINDS` (por defecto `si_no`) dice qué tipos sabe mostrar la PWA. La tarjeta de D5b muestra Sí / No; opciones y texto se activan cuando la PWA los soporte. Así nunca se publica una pregunta que la PWA mostraría mal.

## Máximo una por reporte

1. El agente no publica si ya existe una pregunta con ese `report_id`, también al terminar el LLM (que corre en otro hilo).
2. La resiliencia (N12) retoma la pregunta si el nodo cayó antes de publicarla, solo cuando las reglas piden una. Una pregunta libre sin candidatas no se retoma.
3. Si una partición deja dos, la API y el tablero muestran la más antigua, y cuenta la primera respuesta a cualquiera de ellas.

## Qué ve la PWA

`GET /api/questions/{device_id}` entrega **primero la pregunta del reporte más reciente** y solo las de los últimos **5 minutos** (`QUESTION_TTL_S` en `node/views.py`). Así la tarjeta pregunta por lo que la persona acaba de reportar y no por un reporte de hace horas. Una pregunta vencida no se borra (los eventos son inmutables): deja de ofrecerse, y si llega su respuesta igual se acepta.

El service worker de la PWA (`citizen/sw.js`) usa la red primero y la copia guardada sin red. Con "caché primero", un navegador que ya tenía la PWA seguía con una versión sin la tarjeta de preguntas. `tests/js/citizen_sw.test.mjs` verifica que guarde todos los módulos que carga la app.

## En el tablero

En "por qué está aquí", la respuesta se lee como un dato para los rescatistas:

- Sin respuesta: `recepcion: preguntó al ciudadano «¿Hay personas atrapadas en la casa que se quema?»: esperando respuesta. Motivo: … (agente · gemini/…)`
- Con respuesta: `recepcion: dato del ciudadano: hay personas atrapadas (respondió Sí a «…»). Motivo: … (agente · gemini/…)`

`answer_summary` (en `agents/recepcion.py`) arma ese dato; el triaje y la coordinación pueden usarlo.

## Pendiente

- Que el triaje use la respuesta (`respuesta_seguimiento`, pendiente de N8). Por ejemplo, un "Sí" a `no_respira_o_sangrado` debería subir a crítica, y un "Sí" a `gas_o_cables` debería ser una señal de riesgo.
- Opciones y texto en la tarjeta de la PWA (frente C).
- Mostrar el dato confirmado en la tarjeta, sin abrir el desplegable, y en el "qué decir" de la ficha (API y frente de Oscar).
- Un campo de personas en la PWA, para que `personas` deje de faltar casi siempre.
