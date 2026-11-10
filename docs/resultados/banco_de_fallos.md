# Banco de fallos · semilla 15

Comando: `python -m guadua.bench --semilla 15`. Canal simulado con reloj simulado (sin red real, Docker, Internet ni LLM); una ronda de gossip y resiliencia = 2 s. Con la misma semilla sale la misma tabla, salvo los tiempos marcados como «tiempo de máquina, varía entre corridas». Pérdida «0/30/60 %» = pérdida suelta sin cortes; «cortes» = el perfil tal cual (§5.3). Los retrasos del perfil cuentan en todas las filas, así que también hay desorden.

## Métricas de §15

| Métrica | Valor medido | Objetivo |
| :-- | :-- | :-- |
| Bytes por reporte | SOS escombros 21 B; con texto, promedio 70.1 B y máximo 116 B; 100.0 % caben en 140 B | 21 B y promedio < 140 B |
| Ahorro del diccionario frente a no comprimir | 54.5 % | — |
| Tasa de entrega (emisor → nodo, 30 min) | LoRa 30 %: 100 % · LoRa 60 %: 100 % · LoRa cortes: 100 % | — |
| Latencia p50 / p95 por urgencia | lora 30 %: critica 19 / 37 s · alta 54 / 70 s · media 89 / 128 s · baja 126 / 141 s; espera máx. de los bajos 141 s | lo crítico primero; espera acotada |
| Tiempo por reporte: reglas / clasificador (mediana) | 0.013 ms / 0.800 ms (tiempo de máquina, varía entre corridas) | < 1 ms / < 2 ms |
| Sensibilidad y precisión en críticos (reglas + clasificador) | 94.4 % / 97.1 % en 730 reportes de prueba; matriz abajo | — |
| Duplicados bien vinculados, fusiones erróneas, rumores | pendiente (D4) | — |
| Reportes que llegan al LLM (zona gris) | 18.2 % | cerca de 15 % |
| Casos ambiguos resueltos con LLM en la nube y local | pendiente: requiere LLM (`python -m guadua.agents.triaje_eval`) | — |
| Calidad de la búsqueda del RAG | Hit@3 81 % en 16 preguntas sobre páginas oficiales de la EMRE; 100 % en 22 sobre los resúmenes del equipo | pasaje esperado en el top 3 |
| Tiempo de recuperación de un nodo | lora 30 %: vuelve al día en 14 s, converge en 14 s | — |
| Hashes iguales tras sincronizar | 36 de 36 escenarios | todos |
| Tiempo de cambio de nodo del tablero | pendiente (se mide en el navegador; N14 / D9) | — |
| Tamaño de la miniatura de foto | pendiente (nivel 3) | < 3 KB |

Matriz de confusión (reglas + clasificador, conjunto de prueba):

| real \ publicado | critica | alta | media | baja |
| :-- | --: | --: | --: | --: |
| critica | 170 | 0 | 10 | 0 |
| alta | 4 | 176 | 0 | 0 |
| media | 1 | 3 | 167 | 9 |
| baja | 0 | 0 | 0 | 190 |

## Envío: ráfaga de 60 reportes por la cola (N11), emisor → nodo A

Todos entran a la cola en t = 0 con la urgencia de N7; el emisor transmite una vez por segundo y el nodo responde con ACK. Latencia = hasta que el reporte llega al nodo. Horizonte: 30 min.

| Perfil | Pérdida | Medida | Entregados | Tramas/reporte | critica p50 / p95 | alta p50 / p95 | media p50 / p95 | baja p50 / p95 | Espera máx. baja |
| :-- | :-- | --: | --: | --: | --: | --: | --: | --: | --: |
| wifi | 0 % | 0 % | 60/60 (100 %) | 1.0 | 8 / 16 s | 23 / 30 s | 38 / 45 s | 53 / 60 s | 60 s |
| wifi | 30 % | 35 % | 60/60 (100 %) | 2.3 | 17 / 33 s | 50 / 70 s | 81 / 95 s | 114 / 139 s | 139 s |
| wifi | 60 % | 60 % | 60/60 (100 %) | 6.9 | 36 / 146 s | 133 / 180 s | 244 / 291 s | 329 / 374 s | 374 s |
| wifi | cortes | 3 % | 60/60 (100 %) | 1.1 | 8 / 16 s | 24 / 32 s | 41 / 48 s | 56 / 63 s | 63 s |
| lora | 0 % | 0 % | 60/60 (100 %) | 1.6 | 15 / 26 s | 35 / 47 s | 59 / 71 s | 86 / 98 s | 98 s |
| lora | 30 % | 34 % | 60/60 (100 %) | 2.4 | 19 / 37 s | 54 / 70 s | 89 / 128 s | 126 / 141 s | 141 s |
| lora | 60 % | 59 % | 60/60 (100 %) | 6.8 | 36 / 88 s | 134 / 172 s | 228 / 324 s | 315 / 406 s | 406 s |
| lora | cortes | 42 % | 60/60 (100 %) | 2.9 | 19 / 34 s | 51 / 68 s | 93 / 162 s | 155 / 189 s | 189 s |
| sms | 0 % | 0 % | 60/60 (100 %) | 4.0 | 33 / 76 s | 90 / 124 s | 162 / 182 s | 215 / 244 s | 244 s |
| sms | 30 % | 33 % | 60/60 (100 %) | 4.7 | 32 / 70 s | 111 / 153 s | 190 / 209 s | 254 / 297 s | 297 s |
| sms | 60 % | 60 % | 60/60 (100 %) | 8.4 | 50 / 112 s | 200 / 246 s | 316 / 367 s | 406 / 473 s | 473 s |
| sms | cortes | 3 % | 60/60 (100 %) | 4.1 | 33 / 71 s | 97 / 129 s | 159 / 185 s | 221 / 244 s | 244 s |

## Replicación A → B → C (gossip, N5): 20 reportes que entran por A

«Tomas» = reasignaciones publicadas: un vecino vivo que se dio por caído.

| Perfil | Pérdida | Medida | C tiene todo | Hashes iguales | Rondas | Tramas | Duplicados | Inválidos | Tomas |
| :-- | :-- | --: | --: | --: | --: | --: | --: | --: | --: |
| wifi | 0 % | 0 % | 22 s | 22 s | 11 | 705 | 446 | 0 | 0 |
| wifi | 30 % | 29 % | 40 s | 44 s | 22 | 1339 | 674 | 0 | 0 |
| wifi | 60 % | 61 % | 128 s | 144 s | 72 | 2555 | 664 | 0 | 1 |
| wifi | cortes | 9 % | 24 s | 26 s | 13 | 951 | 606 | 0 | 0 |
| lora | 0 % | 0 % | 22 s | 22 s | 11 | 1726 | 1 | 0 | 0 |
| lora | 30 % | 30 % | 36 s | 46 s | 23 | 3178 | 8 | 0 | 0 |
| lora | 60 % | 60 % | 156 s | 204 s | 102 | 7811 | 54 | 0 | 1 |
| lora | cortes | 36 % | 58 s | 66 s | 33 | 3822 | 24 | 0 | 0 |
| sms | 0 % | 0 % | 124 s | 132 s | 66 | 15722 | 453 | 0 | 0 |
| sms | 30 % | 30 % | 148 s | 168 s | 84 | 14586 | 236 | 0 | 0 |
| sms | 60 % | 60 % | 294 s | 320 s | 160 | 13645 | 79 | 0 | 0 |
| sms | cortes | 5 % | 140 s | 148 s | 74 | 15979 | 410 | 0 | 0 |

## Partición B | C de 60 s con 10 reportes por lado, y reconexión

«Pasos en paralelo» = normalizado o urgencia de un mismo reporte hechos por dos nodos.

| Perfil | Pérdida | Hashes iguales tras unir | Rondas | Tramas | Tomas | Pasos en paralelo |
| :-- | :-- | --: | --: | --: | --: | --: |
| wifi | 0 % | 14 s | 7 | 559 | 0 | 0 |
| wifi | 30 % | 28 s | 14 | 1215 | 0 | 0 |
| wifi | 60 % | 72 s | 36 | 1669 | 1 | 4 |
| wifi | cortes | 16 s | 8 | 632 | 0 | 0 |
| lora | 0 % | 16 s | 8 | 1280 | 0 | 0 |
| lora | 30 % | 44 s | 22 | 3674 | 0 | 0 |
| lora | 60 % | 112 s | 56 | 4851 | 1 | 2 |
| lora | cortes | 44 s | 22 | 3277 | 0 | 0 |
| sms | 0 % | 90 s | 45 | 11753 | 0 | 0 |
| sms | 30 % | 126 s | 63 | 10710 | 0 | 0 |
| sms | 60 % | 180 s | 90 | 11561 | 0 | 0 |
| sms | cortes | 84 s | 42 | 10676 | 0 | 0 |

## Caída de A con 2 reportes sin procesar, toma y vuelta (N12)

Detección y toma cuentan desde la caída; con mucha pérdida B puede llevar segundos sin oír a A desde antes, y la detección sale menor que el plazo de 10 s. «Recuperación» = del regreso de A a ponerse al día con un vecino; «siguió solo» = no oyó a ningún vecino en 10 s y empezó a actuar sin esperar.

| Perfil | Pérdida | Toma | Detección | Toma completa | Pendientes | Recuperación de A | Hashes iguales |
| :-- | :-- | :-- | --: | --: | --: | --: | --: |
| wifi | 0 % | B | 14 s | 18 s | 2 → 0 | 2 s | 10 s |
| wifi | 30 % | B | 14 s | 22 s | 2 → 0 | 2 s | 22 s |
| wifi | 60 % | B | 6 s | 46 s | 2 → 0 | 112 s | 112 s |
| wifi | cortes | B | 14 s | 18 s | 2 → 0 | 2 s | 12 s |
| lora | 0 % | B | 14 s | 18 s | 2 → 0 | 2 s | 10 s |
| lora | 30 % | B | 14 s | 26 s | 2 → 0 | 14 s | 14 s |
| lora | 60 % | B | 4 s | 60 s | 2 → 0 | 56 s | 56 s |
| lora | cortes | B | 14 s | 30 s | 2 → 0 | 2 s | 28 s |
| sms | 0 % | B | 40 s | 62 s | 2 → 0 | 2 s | 50 s |
| sms | 30 % | B | 36 s | 62 s | 2 → 0 | 2 s | 74 s |
| sms | 60 % | B | 22 s | 84 s | 2 → 0 | 2 s | 134 s |
| sms | cortes | B | 38 s | 58 s | 2 → 0 | 2 s | 56 s |

Limitación: datos sintéticos (data/sinteticos/reportes.csv) y perfiles simulados; no son mediciones de equipos reales.
