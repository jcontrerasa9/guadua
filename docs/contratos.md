# Contratos

Un contrato es un acuerdo entre partes que se construyen por separado. Cada contrato tiene su código, sus ejemplos en `tests/fixtures/` y pruebas que lo verifican en cada push. **No se cambia sin que lo acordemos Jacobo y Oscar** (regla 6 de `AGENTS.md`).

| Contrato | Código | Ejemplos | Quién lo usa |
| :---- | :---- | :---- | :---- |
| Cabecera binaria de 21 B | `src/guadua/protocol/header.py`, `citizen/protocol/header.js` | `header_vectors.json` | PWA ↔ nodo |
| Texto comprimido con diccionario (≤ 100 B tras la cabecera) | `src/guadua/protocol/text.py`, `data/diccionario_manizales.txt` | `text_vectors.json` | PWA ↔ nodo |
| Evento (forma general) | `src/guadua/store/events.py` | — | Todos los nodos |
| Catálogo de tipos de evento | `src/guadua/store/event_types.py` | `event_examples.json` | Agentes, nodo, tablero |
| API HTTP del nodo | `src/guadua/node/api.py` | `api_examples.json` | PWA, tablero ↔ nodo |
| Interfaces | `providers/llm.py`, `providers/transport.py`, `knowledge/retriever.py` | — | Agentes, canal, RAG |
| Piso de urgencia | `src/guadua/classifier/urgency.py` | — | Triaje y cualquier agente |

El formato byte por byte de la cabecera está en `docs/diseno.md` §5.2.

**Texto comprimido.** Va después de los 21 B. Sin texto: 0 B. Con texto: 1 B de prefijo (4 bits altos: versión del diccionario, hoy 1; 4 bits bajos: modo, 0 = UTF-8 sin comprimir, 1 = deflate crudo con `data/diccionario_manizales.txt` como `zdict`) seguido de los datos, en total como máximo 100 B. Se usa el modo 1 solo si ocupa menos que el 0. El diccionario se usa byte por byte (su SHA-256 está en `text_vectors.json`); cambiarlo exige subir la versión. JS debe decodificar cada vector a su texto exacto; los bytes comprimidos pueden variar entre implementaciones de zlib.

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

- **`POST /api/messages`** recibe todos los mensajes binarios: reporte, SOS, "Estoy a salvo" y respuesta de seguimiento. El `200` es el ACK. Un mensaje repetido también recibe `200`, con `duplicate: true`, y no se guarda dos veces.
- **El tablero** consulta `GET /api/incidents` cada pocos segundos. Si el nodo no responde, pasa al siguiente de su lista usando `GET /api/health`.
- **Mientras N6 no exista**, la PWA y el tablero usan `tests/fixtures/api_examples.json` como respuestas simuladas.

## Catálogo de tipos de evento

Cada nodo valida un evento con `validate_event` antes de guardarlo. El tipo fija la categoría y el formato del `payload`.

| Tipo | Categoría | Lo produce | Contenido principal | Nivel |
| :---- | :---- | :---- | :---- | :---- |
| `reporte` | hecho | Nodo que recibe el mensaje | Campos de la cabecera, texto, canal, bytes originales | 1 |
| `reporte_normalizado` | derivado | Agente 1 · recepción (o reglas simples mientras no exista) | Tipo, personas, ubicación con radio, señales vitales y de riesgo, campos desconocidos | 1 |
| `urgencia_estimada` | derivado | Agente 2 · triaje | Nivel y piso (nivel ≥ piso), fuente, probabilidades, reglas, verificar ya, ambiguo, pasos | 1 |
| `incidente_agrupado` | derivado | Agente 3 · correlación | Incidente, reportes, vínculo seguro / dudoso / nuevo, confianza, rumor, contradicciones | 1 |
| `ficha` | derivado | Agente 6 · coordinación | Responsable, unidades, advertencia de riesgo, mensaje, citas, si usó plantilla | 1 |
| `decision` | decisión | Tablero, a través del nodo | Aprobar / editar / rechazar, mensaje editado, operador. `refs[0]` = la ficha | 1 |
| `a_salvo` | hecho | Nodo | Reporte, nombre, barrio | 2 |
| `transcripcion_radio` | hecho | Nodo | Clip, texto, si es observación directa | 2 |
| `respuesta_seguimiento` | hecho | Nodo | Pregunta, respuesta | 2 |
| `confirmacion_campo` | hecho | Nodo | Incidente, confirmado, nota | 2 |

**Cómo se encadenan:** `reporte` → `reporte_normalizado` → `urgencia_estimada` → `incidente_agrupado` → `ficha` → `decision`. Cada evento derivado apunta en `refs` al evento en el que se basa. Los eventos no se editan: si cambia la urgencia o un grupo, se publica un evento nuevo, y vale el más reciente.

**Pendiente de definir** (se agrega al catálogo cuando llegue su tarea): las solicitudes entre agentes del vigía (D6) y las preguntas de seguimiento como evento (D5a).
