# Plan de trabajo

Fuente: `docs/diseno.md` §17 (niveles y reparto) y §18 (decisiones abiertas).
**Regla:** no se empieza un nivel mientras el anterior no corra de punta a punta.

**Responsables:** **Yo** = Integrante 1, backend (Claude Code) · **Compañero** = Integrante 2, interfaces y campo (Antigravity) · **SIN ASIGNAR** = hay que decidirlo (ver el final).

**Cómo se usa:** cada uno toma solo tareas con su nombre. Al empezar, cambia el estado a 🔄; al terminar (criterio cumplido y CI en verde), a ✅. Para tomar una tarea **SIN ASIGNAR**, primero se pone el nombre aquí, en un PR propio.

## Carriles en paralelo

Los carriles no comparten archivos; solo se comunican a través de los contratos (cabecera, eventos, interfaces, `tests/fixtures/`).

| Carril | Responsable | Carpetas | Tareas |
| :---- | :---- | :---- | :---- |
| A · Red y datos | Yo | `protocol/`, `channel/`, `store/`, `services/` | N1, N2, N3, N4 → N5, N6, N11, N12 |
| B · Urgencia | Yo | `classifier/`, `agents/` (triaje) | N7 → N8 |
| C · Interfaces | Compañero | `citizen/`, `dashboard/` | N13, N14 |
| D · Coordinación | SIN ASIGNAR | `agents/` (correlación, coordinación), `knowledge/` | N9, N10 |
| E · Códec JS | SIN ASIGNAR | `citizen/protocol/` | N16 |

Al empezar, A, B y C pueden avanzar al mismo tiempo. Dentro de A, N1, N2 y N3 son independientes entre sí. N15 (banco de fallos) va al final del nivel 1.

## Nivel 1 · Núcleo

| ID | Tarea | Responsable | Depende de | Terminado cuando | Estado |
| :---- | :---- | :---- | :---- | :---- | :---- |
| N0 | Contratos: cabecera 21 B (Python + JS), eventos, interfaces, vectores | Yo | — | Vectores pasan en pytest y `node --test` | ✅ |
| N1 | Texto con diccionario (`zlib zdict`) y diccionario de Manizales en `data/` | Yo | N0 | Vectores de texto en `tests/fixtures/`; ahorro medido frente a no comprimir | ☐ |
| N2 | Fragmentos y ACK con mapa de bits | Yo | N0 | Una foto de 3 KB se reconstruye con 60 % de pérdida reenviando solo lo que falta | ☐ |
| N3 | Canal simulado con perfiles (Wi-Fi, radio/LoRa, SMS) y proxy de fallos | Yo | N0 | Implementa `Transport`; con semilla fija, la pérdida medida coincide con la configurada (±5 %) | ☐ |
| N4 | Registro de eventos en SQLite (WAL) | Yo | N0 | Se agrega a `STORES` en `tests/test_contracts.py` y pasa todas las pruebas de contrato | ☐ |
| N5 | Gossip con vectores de versión, topología `A ↔ B ↔ C` | Yo | N3, N4 | Lo que entra por A llega a C; los 3 hashes coinciden tras 60 % de pérdida | ☐ |
| N6 | Nodo FastAPI: recibe reportes, guarda eventos, sirve las vistas; `docker compose up` | Yo | N4 | `docker compose up` levanta 3 nodos sin Internet ni `.env` | ☐ |
| N7 | Reglas de prioridad mínima + clasificador calibrado + datos sintéticos | Yo | N0 | Reglas < 1 ms y clasificador < 2 ms por reporte; sensibilidad en críticos reportada con conjunto de prueba separado | ☐ |
| N8 | Agente de triaje con ciclo en la zona gris (LangGraph) | Yo | N7 | Con `NullLLM` el caso queda "ambiguo, revisar"; nunca baja el piso (`apply_floor`); máximo 6 pasos | ☐ |
| N9 | Correlación básica (agrupación contra el centro del grupo) | **SIN ASIGNAR** | N4 | Duplicados vinculados sin efecto cadena en datos sintéticos | ☐ |
| N10 | Agente de coordinación con matriz EMRE y aprobación humana | **SIN ASIGNAR** | N4 | Ficha queda pendiente hasta Aprobar / Editar / Rechazar; sin LLM usa plantilla | ☐ |
| N11 | Cola de comunicación con envejecimiento (Agente 4) | Yo | N2, N3 | Críticos salen primero; espera máxima de los bajos medida y acotada | ☐ |
| N12 | Resiliencia: latidos, caída de un nodo, reasignación del dueño, recuperación | Yo | N5 | Al apagar A, B toma sus pendientes; A vuelve y converge; tiempo medido | ☐ |
| N13 | PWA ciudadana: 3 bloques, cola local (IndexedDB), estado Guardado → Enviando → Recibido | Compañero | N0, N6 | Un reporte hecho sin red se envía solo al volver la conexión; usa `citizen/protocol/header.js` | ☐ |
| N14 | Tablero: tarjetas, cambio automático de nodo, mapa offline | Compañero | N6 | Al apagar el nodo conectado, cambia solo al siguiente y lo indica | ☐ |
| N15 | Banco de fallos con métricas (pérdida 0/30/60 %, partición, caída) | Yo | N5, N11, N12 | Un comando produce la tabla de métricas de §15 con semillas fijas | ☐ |
| N16 | Códec JS: texto con diccionario (`pako`) y fragmentos | **SIN ASIGNAR** | N1, N2 | Pasa los mismos vectores de `tests/fixtures/` que Python | ☐ |

## Nivel 2 · Diferenciadores

| ID | Tarea | Responsable | Terminado cuando | Estado |
| :---- | :---- | :---- | :---- | :---- |
| D1 | Modo escombros + baliza acústica (~3000 Hz) | Compañero | Envía un SOS de 21 B y emite pulsos sin mirar la pantalla | ☐ |
| D2a | "Estoy a salvo" en la PWA | Compañero | Mensaje de ~14 B enviado y confirmado | ☐ |
| D2b | Registro de personas a salvo en el nodo y en el tablero | **SIN ASIGNAR** | Cruza a salvo con buscados; visible en el tablero | ☐ |
| D3a | Riesgo para rescatistas en triaje | Yo | Señales de riesgo marcadas en el evento de urgencia sin cambiar la urgencia | ☐ |
| D3b | Riesgo para rescatistas en coordinación | **SIN ASIGNAR** | La ficha recomienda la unidad adecuada (p. ej., HazMat) | ☐ |
| D3c | Franja de advertencia de riesgo en las tarjetas | Compañero | Visible sin abrir el desplegable | ☐ |
| D4 | Confianza y rumores a fondo + correlación agéntica | **SIN ASIGNAR** | 40 copias "me contaron" → confianza baja; 3 testigos directos → alta | ☐ |
| D5a | Preguntas de seguimiento: agente de recepción | **SIN ASIGNAR** | Máximo 1 pregunta por reporte, visible en el tablero | ☐ |
| D5b | Preguntas de seguimiento: tarjeta Sí / No en la PWA | Compañero | Se responde con un toque al volver la señal | ☐ |
| D6 | Agente vigía | **SIN ASIGNAR** | Casos olvidados generan solicitudes; nunca cambia prioridades | ☐ |
| D7 | Radio VHF con `faster-whisper` | Compañero | Clip con dos incidentes → dos reportes ligados al mismo audio | ☐ |
| D8 | RAG híbrido (FTS5 + embeddings) y tabla de recursos | **SIN ASIGNAR** | Implementa `Retriever`; ~20 preguntas, sección esperada en el top 3 | ☐ |
| D9 | Barra de simulación para el jurado | **SIN ASIGNAR** | Cambia perfil, lanza ráfaga, inyecta radio, apaga A y muestra hashes | ☐ |
| D10 | HTTPS en el celular (`adb reverse`, luego `mkcert`) | Compañero | GPS y modo offline funcionan en un teléfono real | ☐ |

## Nivel 3 · Extras (solo si el nivel 2 corre de punta a punta)

Miniatura de foto (< 3 KB) · variante del clasificador con embeddings · detección por agitar el teléfono · mula de datos entre particiones · portal cautivo simulado · varias entidades con índices propios. Todas **SIN ASIGNAR**.

## Entregables finales

Documentos de arquitectura, fichas de agentes (`docs/fichas/`), protocolo y resultados; pitch; ensayo con la red desconectada. **SIN ASIGNAR**.

## ⚠️ Sin responsable

**Ya señalado en §18 del diseño** ("el Integrante 1 concentra demasiado"):

- Agentes de **correlación** (N9, D4), **coordinación** (N10, D3b) y **vigía** (D6).
- **RAG, matriz EMRE y recursos** (N10, D8).
- **Codificador binario en JS** (N16). La cabecera (N0) ya está hecha; falta el texto y los fragmentos.

**Sin responsable y no mencionado en §18:**

- Agente de **recepción** (Agente 1) y sus preguntas de seguimiento (D5a).
- **Registro de personas** a salvo en el nodo (D2b).
- **Barra de simulación** para el jurado (D9).
- **Entregables finales** y extras del nivel 3.

## Decisiones abiertas (de §18)

- [ ] Tope para reportes de segunda mano en la fórmula de confianza.
- [ ] Documentos oficiales (EMRE, PMGRD): qué entra al RAG y qué entidades son reales.
- [ ] Modelo de Gemini en la capa gratuita.
- [ ] Modelo de Ollama para los agentes sin Internet.
- [ ] Modelo de embeddings (multilingüe, pequeño, ONNX).
- [ ] Texto del consejo de autoprotección del modo escombros.
- [ ] Docker disponible en ambos equipos.
- [ ] Criterios de evaluación del jurado.
