# Resultados de pruebas · Guadua

Entregable 6. Métricas, escenarios ejecutados, errores observados, limitaciones y pruebas de estrés. Medido el 10 de noviembre de 2026 sobre la rama `dev`. Las tablas completas del banco de fallos están en [resultados/banco_de_fallos.md](resultados/banco_de_fallos.md).

## 1. Resumen

| Qué se mide | Resultado | Objetivo (diseño §15) |
| :-- | :-- | :-- |
| Tamaño del mensaje | SOS de escombros: **21 B**. Con texto: promedio **70,1 B**, máximo 116 B; **100 %** cabe en un SMS (140 B) | 21 B y promedio < 140 B |
| Compresión del texto (diccionario de Manizales) | **54,5 %** menos que sin comprimir | — |
| Entrega con pérdida (ráfaga de 60 reportes) | **60/60 (100 %)** en los 12 escenarios, incluidos 60 % de pérdida y cortes en Wi-Fi, LoRa y SMS | que el reporte sobreviva |
| Prioridad en la cola | Con LoRa y 30 % de pérdida, lo crítico llega en **19 s** (p50) y lo bajo en 126 s; la espera máxima de lo bajo es acotada (141 s) | lo crítico primero, nada espera indefinidamente |
| Sensibilidad y precisión en críticos | **94,4 % / 97,1 %** (reglas + clasificador, 730 reportes de prueba no vistos) | — |
| Tiempo del piso de urgencia | reglas **0,014 ms** y clasificador **0,77 ms** por reporte (mediana) | < 1 ms y < 2 ms |
| Reportes que llegan al LLM (zona gris) | **18,2 %** | cerca del 15 % |
| Agente de triaje con LLM local (`qwen2.5:7b`) | 40 casos difíciles de la zona gris: **40/40 resueltos**, 0 respuestas inválidas, nivel correcto en **12/40** (las reglas y el clasificador solos: 7/40); 1 paso y **1,7 s** por caso | que la agencia aporte y respete sus límites |
| Convergencia entre nodos | hashes iguales en **36 de 36** escenarios (replicación, partición y caída) | todos |
| Caída y recuperación de un nodo | con LoRa y 30 % de pérdida: B toma el trabajo de A en 26 s; A vuelve al día en **14 s** | — |
| Búsqueda del RAG | Hit@3 **81 %** (13/16) sobre páginas verificadas de los PDF de la EMRE; 100 % (22/22) sobre los resúmenes del equipo | sección esperada en el top 3 |
| Preguntas de seguimiento (recepción) | pertinentes **20/20** con reglas y 20/20 con LLM local (15 redactadas por el agente) | — |
| Separación de transmisiones de radio | exacta **22/23** con reglas; **17/23** con LLM local (número de partes correcto en 22/23 en los dos) | — |
| Pruebas automáticas | **876** pruebas de Python y **208** de JavaScript, todas en verde; `ruff` sin errores | — |

## 2. Cómo reproducir

```bash
pip install -e ".[dev]"
pytest && node --test "tests/js/*.test.mjs"
python -m guadua.bench --semilla 15            # banco de fallos (~45 s, sin red, Docker ni LLM)
python -m guadua.classifier.evaluate           # sensibilidad, precisión, matriz y tiempos
python -m guadua.agents.triaje_eval            # cuánto llega a la zona gris (con LLM: cómo resuelve)
python -m guadua.knowledge.eval_rag            # calidad de la búsqueda del RAG
python -m guadua.agents.recepcion_eval         # preguntas de seguimiento (con --llm, el agente)
python -m guadua.agents.radio_eval --clips     # separación de la radio VHF

# Con LLM local (Ollama corriendo con qwen2.5:7b):
export LLM_PROVIDER=ollama OLLAMA_MODEL=qwen2.5:7b OLLAMA_HOST=http://localhost:11434
python -m guadua.agents.triaje_eval --limite 40
python -m guadua.agents.recepcion_eval --llm
python -m guadua.agents.radio_eval --clips --llm
```

El banco de fallos no usa LLM, así que su fila "Casos ambiguos resueltos con LLM" queda como pendiente en el anexo; esas cifras están en §3.4.

El banco usa un canal y un reloj simulados con semilla fija: con la misma semilla salen las mismas tablas, salvo los tiempos de máquina (reglas y clasificador).

## 3. Escenarios ejecutados

### 3.1 Banco de fallos (36 escenarios)

Tres perfiles de canal (Wi-Fi, radio/LoRa, SMS) × cuatro condiciones (0 %, 30 % y 60 % de pérdida, y cortes con el perfil tal cual), con retrasos y desorden en todos:

| Escenario | Qué se hace | Qué demuestra |
| :-- | :-- | :-- |
| Envío | Ráfaga de 60 reportes en la cola del celular con ACK | Entrega con pérdida y orden por urgencia |
| Replicación | 20 reportes entran por A y viajan a C por B (gossip) | Que todos los nodos terminan con lo mismo |
| Partición | B y C aislados 60 s con 10 reportes por lado, y reconexión | Convergencia tras una partición |
| Caída | A se apaga con 2 reportes sin procesar, B toma su trabajo y A vuelve | Que nada queda huérfano |

### 3.2 Clasificación de urgencia

730 reportes sintéticos de prueba (combinaciones y frases que el clasificador no vio al entrenar). Matriz de confusión (reglas + clasificador; filas = real):

| real \ publicado | crítica | alta | media | baja |
| :-- | --: | --: | --: | --: |
| crítica | 170 | 0 | 10 | 0 |
| alta | 4 | 176 | 0 | 0 |
| media | 1 | 3 | 167 | 9 |
| baja | 0 | 0 | 0 | 190 |

De los 10 críticos que salieron como media, ninguno quedó marcado como ambiguo: el clasificador no entiende significado en frases como "no encontramos a mi hermano, estaba adentro" (ver limitaciones).

### 3.3 Prueba de punta a punta con LLM local

Nodo real (`python -m guadua`) con `qwen2.5:7b` en Ollama y 9 mensajes binarios como los que manda la PWA (10 de noviembre):

| Reporte | Resultado |
| :-- | :-- |
| "Se cayó la casa de dos pisos, mi hermano y mi mamá quedaron atrapados, se oyen gritos" | Crítica por regla. Ficha a Bomberos con el mensaje "Chipre, casa de dos pisos colapsada. Gritos de personas atrapadas." |
| Segundo reporte de colapso en Chipre y un SOS de escombros en Chipre | Incidentes aparte (vínculo dudoso, decide una persona); el tablero los marca como posible mismo hecho |
| "Creo que mi vecino tiene una fuga de gas o tal vez su basura huele muy mal" | Zona gris por duda; el agente confirma alta. Ficha con advertencia de gas y "Evacúen en dirección contraria al viento" |
| "Un señor se cayó de la moto y tiene la pierna rota, está consciente" | El clasificador dijo crítica; el agente bajó a alta con cita textual y el motivo "la persona está consciente y la lesión no es vital" |
| "Hay un gato atrapado en un árbol" (botón Atrapados) | Sin piso vital por ser solo animales; el agente bajó de crítica a alta (un nivel por vez) tras corregir una cita inválida |
| "Sale humo negro de una casa en el centro, hay llamas en el segundo piso" | Crítica por regla de incendio; "Despachar con Máquina extintora" |
| "Dicen que se cayó un árbol grande en la avenida Santander" | Rumor (confianza 10 %); ficha a Infraestructura con "verificar al llegar" |
| "Estoy a salvo" con nombre | Aparece en el registro de personas a salvo del tablero |

Rechazar una ficha de Bomberos produjo una ficha nueva para Defensa Civil con un mensaje nuevo. Aprobar despachos descuenta unidades libres.

### 3.4 Agentes con LLM local

Medido con Ollama y `qwen2.5:7b` en una GPU de 8 GB (10 de noviembre). Gemini no se midió.

| Agente | Conjunto | Resultado |
| :-- | :-- | :-- |
| Triaje | 40 casos del conjunto `ambiguo` que caen en la zona gris | 40 resueltos, ninguno a revisión por falla; ninguna herramienta ni respuesta inválida. Nivel igual al esperado en 12 (N7 tenía 7): 6 subidas y 11 bajadas. 1 paso y 1,7 s por caso |
| Recepción | 20 reportes como los de la PWA | 20/20 preguntas pertinentes; 15 las redactó el agente y 1 salió de reglas (el resto no necesitaba pregunta) |
| Separación de radio | 23 transcripciones (11 guiones y 12 difíciles) | 17/23 exactas frente a 22/23 con reglas. El número de partes es correcto en 22/23; en 3 casos el modelo deja sin decidir si el emisor lo vio o se lo contaron, en 2 dice "atrapados" donde el guion dice "colapso" y en 1 junta dos sucesos en uno |

El agente de triaje mejora lo que reciben las reglas y el clasificador (de 7 a 12 aciertos en casos difíciles) sin violar los límites de la regla 1. Que acierte solo 12 de 40 muestra cuán difícil es el conjunto `ambiguo` y por qué la decisión final es de una persona.

### 3.5 Confianza y rumores (D4)

Verificado con pruebas automáticas (`tests/test_correlacion_confianza.py`): 40 copias idénticas de "me contaron" dan confianza baja y quedan como rumor; 40 relatos distintos de segunda mano no superan el tope de 0,25; 3 testigos directos con redacción distinta dan confianza alta (0,936); un mismo dispositivo repetido no suma; una confirmación de campo sube a 1. La correlación no se fusiona en el mismo barrio sin evidencia (sin efecto cadena; ver `tests/test_correlacion.py`).

## 4. Pruebas de estrés

| Condición | Resultado |
| :-- | :-- |
| Ráfaga de 60 reportes con 60 % de pérdida por SMS | 60/60 entregados; 8,4 tramas por reporte; crítico p50 50 s, bajo p50 406 s |
| Ráfaga de 60 reportes con 60 % de pérdida por LoRa | 60/60; crítico p50 36 s, bajo p50 315 s |
| Replicación de 20 reportes con 60 % de pérdida por SMS | C tiene todo en 294 s; hashes iguales en 320 s |
| Partición de 60 s con 60 % de pérdida por SMS | hashes iguales 180 s después de reconectar |
| Caída de A con 60 % de pérdida por SMS | B lo detecta en 22 s y termina sus pendientes en 84 s; hashes iguales en 134 s |
| 730 reportes por el piso de urgencia | menos de 1 ms por reporte sumando reglas y clasificador |

## 5. Errores observados y corregidos

Durante la revisión de las funcionalidades con el sistema corriendo (9 y 10 de noviembre):

| Error | Causa | Corrección |
| :-- | :-- | :-- |
| El agente de triaje nunca usaba el LLM con "un perro con una herida abierta" | El clasificador estaba seguro (97 %) y no llegaba a la zona gris | Señales de lenguaje (solo animales, duda) que mandan el caso a revisión |
| "Gato atrapado" quedaba en crítica | La palabra "atrapado" fija el piso vital | Excepción: un texto solo de animales no fija piso vital; lo revisa el agente |
| La pregunta de seguimiento no aparecía en la PWA | La PWA recordaba las respondidas por un id que se repite al reiniciar el nodo | La clave incluye el `report_id` |
| "Estoy a salvo" se rechazaba (400) y el registro estaba vacío | El nodo no aceptaba el tipo 4 y `/api/people` no se había conectado | Se acepta el tipo 4 y se sirve la lista |
| La PWA reintentaba para siempre un mensaje rechazado | No distinguía un 400 de una caída | Un 400 es estado final |
| Todas las fichas tenían el mensaje de la plantilla | El agente de coordinación se creaba sin LLM | Redacción con LLM en otro hilo; plantilla si falla |
| Citas del RAG no verificables | El corpus eran resúmenes con secciones inventadas y un documento que no existe; la matriz citaba una página equivocada | Corpus con el texto real de los 9 PDF, citado por página; resúmenes marcados como ejemplo |
| En Docker el RAG estaba vacío y las advertencias de riesgo salían genéricas | El `Dockerfile` no copiaba el corpus ni `riesgos_unidad.csv` | Se copian |
| Las unidades libres no bajaban al aprobar despachos | Salían fijas de la tabla de recursos | Se descuentan los despachos aprobados |
| El mapa no mostraba nada sin GPS | Los barrios no tenían coordenadas | Centro aproximado por barrio; calles de OpenStreetMap con Internet |
| El buscador de personas a salvo solo encontraba el nombre exacto | Comparación exacta | Coincidencia por palabras, prefijos y una letra de diferencia |

## 6. Limitaciones

- **Datos sintéticos y canal simulado.** Los reportes de prueba son generados y los perfiles de canal son simulaciones; no son mediciones con radios o teléfonos reales.
- **El clasificador no entiende significado.** En el conjunto `ambiguo` (318 casos difíciles), 113 salen con un nivel equivocado sin quedar marcados para revisión: combinaciones nuevas de palabras conocidas no llegan al agente. Las reglas vitales siguen fijando el piso.
- **El LLM local acierta poco en los casos más difíciles.** En la zona gris del conjunto `ambiguo`, el agente de triaje deja el nivel esperado en 12 de 40 casos (7 sin él). Es una mejora, no una solución: por eso baja como máximo un nivel y la decisión final es humana. Gemini no se midió. Con Ollama, el agente tardó 1,7 s por caso en la evaluación y de 2 a 23 s en el nodo con varios agentes a la vez.
- **Radio con LLM.** La separación con `qwen2.5:7b` es peor que con reglas (17/23 frente a 22/23): el modelo omite si el emisor lo vio. Conviene que, cuando el LLM no decide ese dato, se use el de las reglas.
- **Un nivel por vez.** Por la regla 1, el agente baja solo un nivel: el gato en el árbol queda en alta y lo termina de ajustar el comandante.
- **Correlación conservadora.** En el mismo barrio no fusiona sin evidencia; el comandante ve posibles duplicados y decide.
- **RAG institucional.** Los PDF de la EMRE dicen quién responde, no cómo actuar en la escena; lo táctico sale de resúmenes del equipo marcados como ejemplo. Hit@3 81 % en páginas oficiales.
- **Ubicación.** Sin HTTPS el celular no entrega GPS (D10 pendiente): se usa el barrio, con radio de 600 m y centro aproximado. Las calles del mapa necesitan Internet.
- **Pendientes del banco:** duplicados bien vinculados como métrica, tiempo de cambio de nodo del tablero (se observa en el navegador) y miniatura de foto (nivel 3).
