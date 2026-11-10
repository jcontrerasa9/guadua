# Plan de trabajo

Fuente: `docs/diseno.md` §17 (niveles y reparto) y §18 (decisiones abiertas).
**Regla:** no se empieza un nivel mientras el anterior no corra de punta a punta.

**Responsables:** **Jacobo** = backend · **Oscar** = interfaces, campo y coordinación · **SIN ASIGNAR** = hay que decidirlo (ver el final). Los responsables son las personas del equipo, no las herramientas que usen.

**Cómo se usa:** cada uno toma solo tareas con su nombre. Al empezar, cambia el estado a 🔄; al terminar (criterio cumplido y CI en verde), a ✅. Para tomar una tarea **SIN ASIGNAR**, primero se pone el nombre aquí, en un PR propio.

**Ramas:** cada tarea sale de `dev` en una rama con su ID (p. ej. `n4-sqlite-store`) y vuelve a `dev` por PR con el CI en verde. `dev` pasa a `main` cuando corre de punta a punta.

## Frentes de trabajo

Un frente es un grupo de tareas que toca sus propias carpetas. Los frentes no comparten archivos; solo se comunican a través de los contratos (cabecera, eventos, interfaces, `tests/fixtures/`), así que avanzan en paralelo.

| Frente | Responsable | Carpetas | Tareas |
| :---- | :---- | :---- | :---- |
| A · Red y datos | Jacobo | `protocol/`, `channel/`, `store/`, `services/` | N1, N2, N3, N4 → N5, N6, N11, N12 |
| B · Urgencia | Jacobo | `classifier/`, `agents/triaje*` | N7 → N8 |
| C · Interfaces | Oscar | `citizen/` (incluido `citizen/protocol/`), `dashboard/` | N13, N14, N16 |
| D · Correlación y coordinación | Oscar | `agents/correlacion*`, `agents/coordinacion*`, `knowledge/` | N9, N10 |

- **Desde el inicio** avanzan A y B (Jacobo) y C o D (Oscar). N9 y N10 no esperan a N4: se desarrollan contra `InMemoryEventStore` de `tests/fakes.py`, que cumple el mismo contrato.
- **N13 y N14 no esperan a N6:** usan `tests/fixtures/api_examples.json` como respuestas simuladas del nodo; al terminar N6 se prueban contra el nodo real.
- **Dentro de A**, N1, N2 y N3 son independientes entre sí. N15 (banco de fallos) cierra el nivel 1.
- **En `agents/`** trabajan los dos, pero cada agente en su propio archivo.

## Nivel 1 · Núcleo

| ID | Tarea | Responsable | Depende de | Terminado cuando | Estado |
| :---- | :---- | :---- | :---- | :---- | :---- |
| N0 | Contratos: cabecera 21 B (Python + JS), eventos, interfaces, vectores | Jacobo | — | Vectores pasan en pytest y `node --test` | ✅ |
| N0b | Contratos: API HTTP del nodo y catálogo de tipos de evento (`docs/contratos.md`) | Jacobo | N0 | Ejemplos de `api_examples.json` y `event_examples.json` pasan en pytest | ✅ |
| N1 | Texto con diccionario (`zlib zdict`) y diccionario de Manizales en `data/` | Jacobo | N0 | Vectores de texto en `tests/fixtures/`; ahorro medido frente a no comprimir | ✅ |
| N2 | Fragmentos y ACK con mapa de bits | Jacobo | N0 | Una foto de 3 KB se reconstruye con 60 % de pérdida reenviando solo lo que falta | ✅ |
| N3 | Canal simulado con perfiles (Wi-Fi, radio/LoRa, SMS) y proxy de fallos | Jacobo | N0 | Implementa `Transport`; con semilla fija, la pérdida medida coincide con la configurada (±5 %) | ✅ |
| N4 | Registro de eventos en SQLite (WAL) | Jacobo | N0 | Se agrega a `STORES` en `tests/test_contracts.py` y pasa todas las pruebas de contrato | ✅ |
| N5 | Gossip con vectores de versión, topología `A ↔ B ↔ C` | Jacobo | N3, N4 | Lo que entra por A llega a C; los 3 hashes coinciden tras 60 % de pérdida | ✅ |
| N6 | Nodo FastAPI: implementa las rutas de nivel 1 de `docs/contratos.md`, valida eventos con `validate_event`, sirve las vistas | Jacobo | N0b, N4 | `docker compose up` levanta 3 nodos sin Internet ni `.env`; cada ruta responde con el formato de `api_examples.json` | ✅ |
| N7 | Reglas de prioridad mínima + clasificador calibrado + datos sintéticos | Jacobo | N0 | Reglas < 1 ms y clasificador < 2 ms por reporte; sensibilidad en críticos reportada con conjunto de prueba separado | ✅ |
| N8 | Agente de triaje con ciclo en la zona gris (LangGraph) | Jacobo | N7 | Con `NullLLM` el caso queda "ambiguo, revisar"; nunca baja el piso (`apply_floor`); máximo 6 pasos | ✅ |
| N9 | Correlación básica (agrupación contra el centro del grupo) | Oscar | N0b | Publica `incidente_agrupado` válido; duplicados vinculados sin efecto cadena en datos sintéticos | ✅ |
| N10 | Agente de coordinación con matriz EMRE y aprobación humana | Oscar | N0b | Publica `ficha` válida que queda pendiente hasta una `decision`; sin LLM usa plantilla | ✅ |
| N11 | Cola de comunicación con envejecimiento (Agente 4) | Jacobo | N2, N3 | Críticos salen primero; espera máxima de los bajos medida y acotada | ✅ |
| N11b | Reintento con espera creciente en la cola de comunicación | Jacobo | N11, N15 | Con la misma semilla del banco bajan las tramas por reporte y la espera máxima de los bajos en SMS, y lo crítico sigue saliendo primero | ✅ |
| N12 | Resiliencia: latidos, caída de un nodo, reasignación del dueño, recuperación | Jacobo | N5 | Al apagar A, B toma sus pendientes; A vuelve y converge; tiempo medido | ✅ |
| N13 | PWA ciudadana: 3 bloques, cola local (IndexedDB), estado Guardado → Enviando → Recibido | Oscar | N0b | Un reporte hecho sin red se envía solo al volver la conexión; usa `citizen/protocol/header.js` | ✅ |
| N14 | Tablero: tarjetas, cambio automático de nodo, mapa offline | Oscar | N0b | Al apagar el nodo conectado, cambia solo al siguiente y lo indica | ✅ |
| N15 | Banco de fallos con métricas (pérdida 0/30/60 %, partición, caída) | Jacobo | N5, N11, N12 | Un comando produce la tabla de métricas de §15 con semillas fijas | ✅ |
| N16 | Códec JS: texto con diccionario (`pako`) y fragmentos | Oscar | N1, N2 | Pasa los mismos vectores de `tests/fixtures/` que Python | ✅ |

### Pendiente de N8 (agente de triaje)

Lo que N8 dejó fuera a propósito y en qué tarea va. Ficha completa: `docs/fichas/triaje.md`.

| Pendiente | Va con | Qué falta |
| :---- | :---- | :---- |
| Herramienta `radio_en_zona` | D7 | Que existan eventos `transcripcion_radio`; luego se agrega a `TOOLS` en `agents/triaje.py` |
| Herramienta `respuesta_seguimiento` | Tarea nueva (frente B), tras D5a ✅ | Ya hay preguntas y respuestas (`docs/fichas/recepcion.md`). Falta que el triaje las lea: un "Sí" a `no_respira_o_sangrado` sube a crítica; un "Sí" a `gas_o_cables` es señal de riesgo |
| Herramienta `solicitar_verificacion_campo` | D6 o tarea nueva | Un tipo de evento nuevo (cambio de contrato). Mientras tanto, `marcar_verificar_ya` |
| Disparador por palabras de duda ("no sé", "creo", "dicen"…) | Tarea nueva, **SIN ASIGNAR** (frente B) | Hoy N8 solo ve lo que N7 marca `ambiguous`: en el conjunto `ambiguo`, 176 de 318 casos salen mal sin marcar. Calibrar con un conjunto distinto de `ambiguo` |
| ~~Retomar casos que el agente no terminó porque el nodo se cayó~~ | N12 ✅ | Al volver, el nodo retoma el triaje cortado; si otro nodo tomó el caso, lo hace ese (`docs/fichas/resiliencia.md`) |
| Elegir los modelos de Gemini y Ollama | Decisión abierta (diseño §18) | Medir con `python -m guadua.agents.triaje_eval --todos` |

## Nivel 2 · Diferenciadores

| ID | Tarea | Responsable | Terminado cuando | Estado |
| :---- | :---- | :---- | :---- | :---- |
| D1 | Modo escombros + baliza acústica (~3000 Hz) | Oscar | Envía un SOS de 21 B y emite pulsos sin mirar la pantalla | ✅ |
| D2a | "Estoy a salvo" en la PWA | Oscar | Mensaje de ~14 B enviado y confirmado | ✅ |
| D3a | Riesgo para rescatistas en triaje | Jacobo | Señales de riesgo marcadas en el evento de urgencia sin cambiar la urgencia | ✅ |
| D2b | Registro de personas a salvo en el nodo y en el tablero | Oscar | Cruza a salvo con buscados; visible en el tablero | ✅ |
| D3b | Riesgo para rescatistas en coordinación | Oscar | La ficha recomienda la unidad adecuada (p. ej., HazMat) | ✅ |
| D3c | Franja de advertencia de riesgo en las tarjetas | Oscar | Visible sin abrir el desplegable | ✅ |
| D4 | Confianza y rumores a fondo + correlación agéntica | Oscar | 40 copias "me contaron" → confianza baja; 3 testigos directos → alta | ✅ |
| D5a | Preguntas de seguimiento: agente de recepción | Jacobo | Máximo 1 pregunta por reporte, visible en el tablero | ✅ |
| D5b | Preguntas de seguimiento: tarjeta Sí / No en la PWA | Oscar | Se responde con un toque al volver la señal | ✅ |
| D6 | Agente vigía | **SIN ASIGNAR** | Casos olvidados generan solicitudes; nunca cambia prioridades | ☐ |
| D7 | Radio VHF con `faster-whisper` | Oscar | Clip con dos incidentes → dos reportes ligados al mismo audio | ☐ |
| D8 | RAG híbrido (FTS5 + embeddings) y tabla de recursos | **SIN ASIGNAR** | Implementa `Retriever`; ~20 preguntas, sección esperada en el top 3 | ☐ |
| D9 | Barra de simulación para el jurado | Oscar | Cambia perfil, lanza ráfaga, inyecta radio, apaga A y muestra hashes | ☐ |
| D10 | HTTPS en el celular (`adb reverse`, luego `mkcert`) | Oscar | GPS y modo offline funcionan en un teléfono real | ☐ |

## Nivel 3 · Extras (solo si el nivel 2 corre de punta a punta)

Miniatura de foto (< 3 KB) · variante del clasificador con embeddings · detección por agitar el teléfono · mula de datos entre particiones · portal cautivo simulado · varias entidades con índices propios. Todas **SIN ASIGNAR**.

## Entregables finales

Documentos de arquitectura, fichas de agentes (`docs/fichas/`), protocolo y resultados; pitch; ensayo con la red desconectada. **SIN ASIGNAR**.

## ⚠️ Sin responsable

Se asignan al terminar el nivel 1, según cómo vaya cada frente:

- Agente **vigía** (D6).
- **RAG y tabla de recursos** (D8).
- **Entregables finales** y extras del nivel 3.

## Decisiones abiertas (de §18)

- [x] Tope para reportes de segunda mano en la fórmula de confianza (fijado en 0.25 para mantener rumores en confianza baja).
- [ ] Documentos oficiales (EMRE, PMGRD): qué entra al RAG y qué entidades son reales.
- [ ] Modelo de Gemini en la capa gratuita.
- [ ] Modelo de Ollama para los agentes sin Internet.
- [ ] Modelo de embeddings (multilingüe, pequeño, ONNX).
- [ ] Texto del consejo de autoprotección del modo escombros.
- [ ] Docker disponible en ambos equipos.
- [ ] Criterios de evaluación del jurado.
