# Agente 1 · Recepción (preguntas de seguimiento y radio VHF)

Código: `src/guadua/agents/recepcion.py` (D5a) y `src/guadua/agents/recepcion_radio.py` (D7). Contrato: `pregunta_seguimiento`, `respuesta_seguimiento`, `transcripcion_radio` y el `reporte` por radio en `docs/contratos.md`. Diseño: §3.1, §6.4, §7 y §10.

Hoy el agente hace dos cosas: las **preguntas de seguimiento** y **separar transmisiones de radio** (abajo, "Radio VHF"). La normalización sigue con "reglas simples" en `node/pipeline.py`; interpretar referencias ("frente a la iglesia de Chipre") llega en una tarea posterior.

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

## Radio VHF (D7)

En el Escenario 2 los celulares están apagados pero la radio de Bomberos funciona. Una transmisión puede contar varios sucesos: *"Estamos en el sitio, incendio en la 23 del Centro. Nos comentan que hay gente atrapada en El Cable"*. El agente la divide en un `reporte` por suceso, ligados al mismo audio, y cada uno sigue la cadena de siempre (urgencia, grupo, ficha, aprobación).

| Campo | Agente 1 · Recepción · `separar_transmision` |
| :---- | :---- |
| Objetivo | Que cada suceso de una transmisión de radio sea su propio reporte, con su lugar y si lo vieron o se lo contaron, sin inventar ni perder nada |
| Entradas | Una `transcripcion_radio` vigente del propio nodo (o de uno que tomó, N12) |
| Salidas | De 1 a 4 eventos `reporte` con `channel="vhf"`, `refs[0]` = la transcripción, el mismo `audio_id`, `report_id` = CRC32 del id de la transcripción + número de parte, y `split_by` (`reglas` o el modelo) |
| Herramientas | `separar_transmision` es el propio evento. El LLM no usa herramientas: responde un solo JSON con las partes |
| Memoria | Ninguna propia; el estado vive en el registro de eventos |
| Límites | Cada parte es un pedazo literal de la transcripción. Barrio solo si se nombró; "directo" solo si alguien dijo que estaba en el sitio; personas solo con número dicho. Toda señal vital o de riesgo y todo barrio de la transcripción deben quedar en alguna parte. Máximo 4 partes y una llamada al LLM, solo si las reglas ven 2 o más sucesos. Por radio no hay preguntas de seguimiento |
| Errores | Sin LLM, si la cadena falla, si responde algo fuera del protocolo o que no pasa el validador: las reglas, con el motivo en el log. Sin transcriptor o con audio ininteligible: el clip queda pendiente y el operador escribe el texto. Nunca tumba el nodo |
| Terminación | Reportes publicados. Si el nodo cae antes, la resiliencia retoma la separación (`separar`) |

### Del audio al reporte

1. **Entrada.** `POST /api/radio` con el audio (wav, ogg, mp3, webm…; hasta 8 MB), el script `python -m guadua.radio enviar clip.ogg`, el botón "Subir clip" del tablero o `POST /api/radio/demo/{clip_id}` con un clip de `data/radio/` (barra de simulación, D9). El id del clip son 16 hex del SHA-256: el mismo audio otra vez responde `duplicate: true`.
2. **Transcripción local.** `faster-whisper` `small` cuantizado a int8, en CPU, en otro hilo, en español, con los barrios en el `initial_prompt` y filtro de voz (VAD). El modelo se baja una vez (`python -m guadua.radio descargar-modelo small`, o al construir la imagen de Docker) y se lee de disco: nunca se descarga al arrancar. El audio queda en el nodo que lo recibió (no viaja por gossip); la transcripción sí, como evento.
3. **Separación** (reglas y, si hace falta, LLM; abajo).
4. **Normalización.** Lo que la radio no dijo queda en `unknown_fields`: sin lugar, toda la ciudad (radio de 5 km); sin decir si lo vieron, cuenta como segunda mano.

### Reglas

- Corta por frases, comas, "y" y conectores ("además", "también", "por otro lado").
- Abre una parte nueva con un suceso en **otro barrio**, con un conector que trae otro tipo de suceso o dice "otro", o con un barrio nuevo de una escena incompatible. Lo que no cuenta un suceso propio vuelve a la parte anterior. Atrapados con colapso o incendio, y un herido con cualquier cosa, se tratan como la misma escena.
- **Tipo:** el primero que se menciona en la parte (atrapados, incendio, colapso, fuga de gas, herido, vía bloqueada; si no, otro).
- **Directo o de segunda mano:** vale la pista más cercana antes del suceso ("estamos en", "llegamos", "vemos", "confirmamos" frente a "nos comentan", "reportan", "al parecer", "la comunidad dice"). Sin pista, desconocido. Una parte sin pista en la misma frase que un "nos dicen que" hereda la segunda mano; nunca hereda "directo", porque subiría el peso sin evidencia.

### LLM

La misma cadena Gemini → Ollama → reglas. Se llama solo si las reglas ven 2 o más sucesos, para cuidar la cuota que se comparte con el triaje. El validador fijo rechaza: texto que no está en la transcripción, tipo desconocido, barrio que no se nombró, "directo" sin que nadie dijera que estaba en el sitio, más de 4 partes, o **perder una señal** (si el LLM omite "atrapados", el piso podría bajar).

### Directo o de segunda mano: para qué sirve

La correlación ya pesa la radio directa en 0,85 y la de segunda mano en 0,10 (diseño §12). Las partes de un mismo clip comparten "dispositivo" (CRC32 de la transcripción), así que no cuentan como testigos independientes entre sí. Una parte de segunda mano con atrapados queda crítica igual (el piso sale del texto), pero con confianza baja y "Posible rumor".

### Respaldo manual

En el panel "📻 Radio VHF" del tablero el operador escucha el audio y escribe o corrige el texto. La corrección es una `transcripcion_radio` nueva (`source="operador"`, `refs[0]` = la anterior): se vuelve a separar, y los reportes de la versión anterior se ocultan del tablero y de `radio_en_zona` (siguen en el registro).

### Evaluación

```bash
python -m guadua.agents.radio_eval                 # reglas, sobre texto
python -m guadua.agents.radio_eval --clips         # + Whisper con los clips de data/radio/
docker exec guadua-node-a-1 python -m guadua.agents.radio_eval --clips --llm
```

23 casos: los 11 guiones de `data/radio/clips.json` y 12 difíciles escritos sin ajustar las reglas. Con reglas (9 de noviembre de 2026): 22/23 exactos (11/12 difíciles). El que falla son dos sucesos en el mismo barrio ("en el Centro tenemos dos casos: un incendio… y un herido…"): las reglas los juntan a propósito y ahí ayuda el LLM. Whisper `small` int8 con 2 hilos, en un equipo de 12 núcleos: unos 3 s por clip de 8–12 s (voz sintética con filtro de radio). Falta medir con los clips grabados por el equipo.

### Pendiente

- Grabar los clips de `data/radio/` (guía en `data/radio/README.md`) y medir el WER real.
- La correlación todavía cuenta los reportes de una transcripción corregida al agrupar (la confianza puede subir un poco); el tablero ya los oculta.
- Medir la separación con el LLM real (`--llm`) e incluir casos de jerga y ruido.
