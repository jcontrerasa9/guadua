# Contratos

Un contrato es un acuerdo entre partes que se construyen por separado. Cada contrato tiene su código, sus ejemplos en `tests/fixtures/` y pruebas que lo verifican en cada push. **No se cambia sin que lo acordemos Jacobo y Oscar** (regla 6 de `AGENTS.md`).

| Contrato | Código | Ejemplos | Quién lo usa |
| :---- | :---- | :---- | :---- |
| Cabecera binaria de 21 B | `src/guadua/protocol/header.py`, `citizen/protocol/header.js` | `header_vectors.json` | PWA ↔ nodo |
| Texto comprimido con diccionario (≤ 100 B tras la cabecera) | `src/guadua/protocol/text.py`, `data/diccionario_manizales.txt` | `text_vectors.json` | PWA ↔ nodo |
| Fragmentos y ACK con mapa de bits | `src/guadua/protocol/fragments.py` | `fragment_vectors.json` | Nodo ↔ nodo, PWA ↔ nodo |
| Mensajes de gossip (vector y evento) | `src/guadua/store/gossip.py` | — | Nodo ↔ nodo |
| Evento (forma general) | `src/guadua/store/events.py` | — | Todos los nodos |
| Catálogo de tipos de evento | `src/guadua/store/event_types.py` | `event_examples.json` | Agentes, nodo, tablero |
| API HTTP del nodo | `src/guadua/node/api.py` | `api_examples.json` | PWA, tablero ↔ nodo |
| Interfaces | `providers/llm.py`, `providers/transport.py`, `providers/transcriber.py`, `knowledge/retriever.py`, `radio/clips.py` (`ClipStore`) | — | Agentes, canal, radio, RAG |
| Piso de urgencia | `src/guadua/classifier/urgency.py` | — | Triaje y cualquier agente |

El formato byte por byte de la cabecera está en `docs/diseno.md` §5.2.

**Texto comprimido.** Va después de los 21 B. Sin texto: 0 B. Con texto: 1 B de prefijo (4 bits altos: versión del diccionario, hoy 1; 4 bits bajos: modo, 0 = UTF-8 sin comprimir, 1 = deflate crudo con `data/diccionario_manizales.txt` como `zdict`) seguido de los datos, en total como máximo 100 B. Se usa el modo 1 solo si ocupa menos que el 0. El diccionario se usa byte por byte (su SHA-256 está en `text_vectors.json`); cambiarlo exige subir la versión. JS debe decodificar cada vector a su texto exacto; los bytes comprimidos pueden variar entre implementaciones de zlib.

**Fragmentos y ACK.** Si un mensaje no cabe en la trama (`Transport.mtu`), se parte en fragmentos. Big-endian.

| Bytes | Fragmento | ACK |
| :-- | :-- | :-- |
| 0 | Versión + tipo: 2 fragmento · 8 fragmento de foto | Versión + tipo: 3 ACK |
| 1–4 | Dispositivo | Dispositivo del mensaje confirmado |
| 5–8 | Consecutivo | Consecutivo del mensaje confirmado |
| 9 | Índice (desde 0) | Tipo confirmado (cualquiera menos ACK) |
| 10 | Total (1 a 255) | Total |
| 11… | Datos (al menos 1 B, hasta `mtu − 11`) | Mapa de bits de ⌈total/8⌉ B: el fragmento `i` es el bit `i % 8` del byte `i // 8`, primero el menos significativo; relleno en cero |

- El **tipo confirmado** separa el texto de la foto de un mismo reporte, que comparten ID.
- Un mensaje que cabe en una trama no se fragmenta y se confirma con total 1 y mapa `01`.
- El emisor reenvía solo los fragmentos que ningún ACK ha confirmado. Un fragmento repetido no se guarda dos veces.
- **Se rechaza** un fragmento sin datos, con índice ≥ total o total 0, y un ACK con mapa de otro tamaño o relleno distinto de cero.

**Respuesta de seguimiento (tipo 7).** Cabecera de 21 B con tipo 7; de ella solo cuentan el dispositivo, el consecutivo (el ID del mensaje, como en un reporte) y la hora. Le sigue:

| Bytes | Contenido |
| :-- | :-- |
| 0 | Respuesta: 0 No · 1 Sí · 2 No sé · `0x10 + i` opción `i` (0 a 3) · `0x20` texto |
| 1… | Con 0, 1, 2 u opción: el `question_id` en UTF-8 (1 a 32 B), hasta el final |
| 1 · 2… | Con texto: 1 B con el largo del `question_id` (1 a 32), el `question_id` y después el texto, con el formato del texto comprimido (sin texto vacío) |

- La pregunta debe ser del mismo dispositivo que responde, y la respuesta, de su tipo: una opción que existe, texto solo si la pregunta es de texto. Si el nodo aún no conoce la pregunta (no le llegó por gossip), acepta la respuesta.
- Una segunda respuesta a la misma pregunta recibe `200` con `duplicate: true` y no se guarda.

**Gossip.** Los nodos se sincronizan con mensajes de tipo 5 ("lote de eventos"): el byte 0 es `0x15` y le sigue un mapa MessagePack.

| Mensaje | Mapa | Para qué |
| :-- | :-- | :-- |
| Vector | `{"k": "v", "vv": {"A": 12, "B": 8}, "d": <8 B>}` | Lo que tiene el nodo: por origen, el último consecutivo sin huecos, y los primeros 8 bytes de su `digest()`. Si el digest coincide, no se envía nada |
| Evento | `{"k": "e", "ev": {…}}` | Un evento que le falta al vecino, como `Event.model_dump(mode="json")` |

- Un mensaje que no cabe en la trama se parte con los fragmentos de arriba. El ID del fragmento sale del ID del evento: dispositivo = CRC32 del nodo de origen, consecutivo = `seq`. Así el mismo evento, reenviado en otra ronda, completa los pedazos que faltaban.
- Cada evento recibido se valida con `validate_event` antes de guardarlo. Lo que no se puede leer o no cumple el catálogo se descarta y se cuenta.
- No hay ACK: lo que se pierde se vuelve a pedir en la ronda siguiente, porque el vector sigue mostrando el hueco.
- Se rechazan los campos desconocidos y cualquier `k` distinto de `"v"` o `"e"`.

## Convenciones

- **Nombres de campos y rutas en inglés** (`report_id`, `/api/incidents`); **valores del dominio en español** (`"aprobar"`, `"reglas"`, `"hecho"`).
- **Enumeraciones como enteros**, con los mismos números en Python y JS:
  - Urgencia: 1 baja · 2 media · 3 alta · 4 crítica.
  - Tipo de incidente y personas: los de la cabecera (§5.2).
- **Horas:** segundos Unix (`float`) según el reloj del nodo. La hora del dispositivo nunca ordena nada.
- **Identificadores:**
  - Reporte: dispositivo en 8 dígitos hex + `-` + consecutivo, p. ej. `a1b2c3d4-7`.
  - Evento en JSON: `nodo:consecutivo`, p. ej. `B:31`.
  - Incidente: texto opaco que asigna la correlación. Mientras no exista, es el `report_id` del primer reporte.
- **Ubicación:** exactamente uno de `geohash` o `barrio`, más `radius_m`. El mapa dibuja un círculo, no un punto.
- **Campos desconocidos se rechazan.** Agregar un campo es cambiar el contrato.

## API HTTP del nodo

Todas las rutas cuelgan de `/api`. Los errores 4xx devuelven `ApiError` (`{"error": "..."}`).

| Método y ruta | Petición | Respuesta | La usa | Nivel |
| :---- | :---- | :---- | :---- | :---- |
| `GET /api/health` | — | `Health` | PWA, tablero | 1 |
| `POST /api/messages` | bytes: cabecera de 21 B + texto | 200 `MessageAck` · 400 | PWA | 1 |
| `GET /api/incidents` | — | `IncidentList` | tablero | 1 |
| `GET /api/incidents/{incident_id}` | — | `IncidentDetail` · 404 | tablero | 1 |
| `POST /api/decisions` | `DecisionRequest` | 201 `DecisionAck` · 404 · 409 | tablero | 1 |
| `GET /api/questions/{device_id}` | — | `QuestionList` | PWA | 2 |
| `GET /api/people` | — | `PeopleList` | tablero | 2 |
| `POST /api/radio` | bytes: el audio, con su `Content-Type` (`audio/*`) | 200 `RadioAck` · 400 | script, tablero | 2 |
| `POST /api/radio/demo/{clip_id}` | — (clip de `data/radio/`) | 200 `RadioAck` · 404 | barra de simulación | 2 |
| `GET /api/radio` | — | `RadioList` | tablero | 2 |
| `GET /api/radio/{audio_id}/audio` | — | 200 bytes `audio/*` · 404 | tablero | 2 |
| `POST /api/radio/{audio_id}/transcription` | `TranscriptionRequest` | 201 `RadioAck` · 404 | tablero | 2 |
| `GET /api/digest` | — | `Digest` | barra de simulación | 2 |

- **`GET /api/questions/{device_id}`** (dispositivo en 8 dígitos hex) devuelve las preguntas sin responder de ese dispositivo, como máximo una por reporte, **la del reporte más reciente primero** y solo las de los últimos 5 minutos (desde que el nodo recibió el reporte). Una pregunta vencida no se borra: deja de ofrecerse, y si llega su respuesta igual se acepta. "No sé" siempre se ofrece, aunque no esté en `options`.
- **`POST /api/messages`** recibe todos los mensajes binarios: reporte, SOS, "Estoy a salvo" y respuesta de seguimiento. El `200` es el ACK. Un mensaje repetido también recibe `200`, con `duplicate: true`, y no se guarda dos veces.
- **Radio VHF (D7).** `POST /api/radio` guarda el audio (hasta 8 MB; wav, ogg, mp3, mp4, webm, flac), lo transcribe con faster-whisper en otro hilo y responde cuando termina: `transcrito` con el texto y el id de la `transcripcion_radio`, o `pendiente` con `note` (sin transcriptor o audio ininteligible). El `audio_id` son los primeros 16 hex del SHA-256 del audio: el mismo audio otra vez responde `duplicate: true` sin transcribir de nuevo (si quedó pendiente, se vuelve a intentar). El audio queda en el nodo que lo recibió (`has_audio` en `RadioList`); la transcripción viaja por gossip. `POST /api/radio/{audio_id}/transcription` publica una transcripción del operador; si ya había una, la corrige (la anterior en `refs[0]`).
- **`GET /api/digest`** (D9) devuelve la huella del registro: `node`, `digest` (el SHA-256 de `EventStore.digest()`) y `events` (cuántos eventos tiene). Si coincide en los tres nodos, convergieron.
- **Rutas de demostración (D9), fuera de `/api` y fuera del contrato.** En el nodo, `GET /_sim/power` y `POST /_sim/power` con `{"on": false}` o `{"on": true}`: apagado, el nodo responde **503** en todo `/api/*` y su gossip calla (ni envía ni atiende tramas), así que los vecinos lo dan por caído y toman su trabajo como en una caída real (N12). Al encenderlo se pone al día antes de dar a otro por caído. Las pantallas siguen servidas. En el proxy de fallos, `POST /_sim/burst` con `{"count": 1…50}` envía reportes sintéticos al nodo con los fallos del perfil activo y responde `{"sent", "delivered", "lost", "profile"}`. Las rutas de control del proxy (`/_channel/*`, `/_sim/*`) tienen CORS abierto.
- **El tablero** consulta `GET /api/incidents` cada pocos segundos. Si el nodo no responde, pasa al siguiente de su lista usando `GET /api/health`.
- **Mientras N6 no exista**, la PWA y el tablero usan `tests/fixtures/api_examples.json` como respuestas simuladas.

## Catálogo de tipos de evento

Cada nodo valida un evento con `validate_event` antes de guardarlo. El tipo fija la categoría y el formato del `payload`.

| Tipo | Categoría | Lo produce | Contenido principal | Nivel |
| :---- | :---- | :---- | :---- | :---- |
| `reporte` | hecho | Nodo que recibe el mensaje; por radio, el Agente 1 al separar la transcripción | Campos de la cabecera, texto, canal, bytes originales. Por radio (`vhf`): además `audio_id` y `split_by`; sin bytes. `refs[0]` = la transcripción | 1 |
| `reporte_normalizado` | derivado | Agente 1 · recepción (o reglas simples mientras no exista) | Tipo, personas, ubicación con radio, señales vitales y de riesgo, campos desconocidos | 1 |
| `urgencia_estimada` | derivado | Agente 2 · triaje | Nivel y piso (nivel ≥ piso), fuente, probabilidades, reglas, verificar ya, ambiguo, pasos | 1 |
| `incidente_agrupado` | derivado | Agente 3 · correlación | Incidente, reportes, vínculo seguro / dudoso / nuevo, confianza, rumor, contradicciones | 1 |
| `ficha` | derivado | Agente 6 · coordinación | Responsable, unidades, advertencia de riesgo, mensaje, citas, si usó plantilla | 1 |
| `decision` | decisión | Tablero, a través del nodo | Aprobar / editar / rechazar, mensaje editado, operador. `refs[0]` = la ficha | 1 |
| `a_salvo` | hecho | Nodo | Reporte, nombre, barrio | 2 |
| `transcripcion_radio` | hecho | Nodo que recibe el audio, o el operador desde el tablero | Clip (`audio_id`), texto, si algo se vio en persona, fuente (`whisper` / `operador`), modelo, duración, confianza, operador. Una corrección lleva la anterior en `refs[0]` | 2 |
| `pregunta_seguimiento` | derivado | Agente 1 · recepción | Reporte, clave del catálogo o `libre`, tipo (Sí / No, opciones, texto), texto, opciones, motivo, fuente (reglas / agente) y modelo. `refs[0]` = el `reporte_normalizado` | 2 |
| `respuesta_seguimiento` | hecho | Nodo | Pregunta (`question_id` = id de la pregunta), respuesta: sí / no / no sé / opción / texto. `refs[0]` = la pregunta | 2 |
| `confirmacion_campo` | hecho | Nodo | Incidente, confirmado, nota | 2 |
| `reasignacion` | hecho | Agente 5 · resiliencia, en el nodo que toma el trabajo | Nodo caído, hasta qué consecutivo, segundos sin latidos. `refs[0]` = `(from_node, up_to_seq)` | 1 |
| `solicitud_vigia` | derivado | Agente 7 · vigía | Incidente, a quién va (`triaje` / `coordinacion` / `tablero`), acción (`reevaluacion_triaje`, `crear_ficha`, `revisar_demora`, `alerta_olvido`), motivo, urgencia que ya tenía (solo para leer), segundos sin atender, reportes nuevos. `refs[0]` = el `incidente_agrupado` revisado | 2 |

**Cómo se encadenan:** `reporte` → `reporte_normalizado` → `urgencia_estimada` → `incidente_agrupado` → `ficha` → `decision`. Cada evento derivado apunta en `refs` al evento en el que se basa. Los eventos no se editan: si cambia la urgencia o un grupo, se publica un evento nuevo, y vale el más reciente.

**Dueño del trabajo:** lo que dispara un evento lo hace su `origin_node`, salvo que una `reasignacion` lo cubra (mismo origen que `from_node` y `seq ≤ up_to_seq`). En ese caso lo hace quien la publicó, y si hay varias, el de id más bajo. Una reasignación nunca la publica el propio `from_node`. Detalle en `docs/fichas/resiliencia.md`.

**Preguntas de seguimiento (D5a).** Como máximo **una por reporte**: el agente de recepción no publica una `pregunta_seguimiento` si ya hay una con el mismo `report_id`. Si una partición deja dos, vale la más antigua (hora de recepción y luego id) y cualquiera de las dos respuestas cuenta. Vale la primera respuesta de cada pregunta. Una respuesta nunca baja la urgencia (regla 1): es evidencia para el triaje y el operador.

**Radio VHF (D7).** El Agente 1 separa cada `transcripcion_radio` vigente (la que ninguna corrección reemplaza) en 1 a 4 `reporte` con `channel="vhf"`, uno por suceso:

- `report_id` = CRC32 del id de la transcripción ("A:12") en 8 hex + `-` + número de parte (1, 2…). Las partes de un clip comparten "dispositivo", y una corrección produce ids nuevos.
- `audio_id` liga los reportes al mismo clip; `refs[0]` es la transcripción; `split_by` dice quién separó (`reglas` o el modelo, p. ej. `gemini/…`).
- `msg_type` = 0 (reporte), `raw_hex` vacío, `flags` solo con lo dicho (observación directa si alguien dijo que estaba en el sitio; atrapados, no respira o sangrado, incendio si el texto lo dice), `people` solo con un número dicho (si no, "no sé"), `barrio` solo si se nombró (si no, `geohash` y `barrio` van en None y la normalización usa toda la ciudad con radio de 5 km, con `location` en `unknown_fields`).
- Por radio no hay preguntas de seguimiento. Si el operador corrige la transcripción, los reportes de la anterior siguen en el registro y el tablero los oculta.

**Vigía (D6).** El nodo corre una ronda cada `SARD_VIGIA_INTERVAL` s (30). Publica una `solicitud_vigia` por cada caso olvidado del que es dueño (el de su `incidente_agrupado` más reciente), **como máximo una por incidente y acción**. Una solicitud **nunca cambia la urgencia** (regla 1): el `level` que lleva es el que el caso ya tenía. Cada acción va a un solo destino: `reevaluacion_triaje` → triaje; `crear_ficha` y `revisar_demora` → coordinación; `alerta_olvido` → tablero. El tablero las muestra en "por qué está aquí".
