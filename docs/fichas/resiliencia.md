# Agente 5 · Resiliencia

Código: `src/guadua/services/resilience.py` (N12) y `src/guadua/services/failover.py` (medición, la reutiliza N15). Pruebas: `tests/test_resilience.py`. Diseño: §9 y §10. Es un servicio **sin LLM**: ningún LLM decide sobre la infraestructura.

| Campo | Agente 5 · Resiliencia |
| :---- | :---- |
| Objetivo | Que ningún caso quede a medias porque se cayó el nodo que lo procesaba, y que el nodo que vuelve converja sin duplicar trabajo |
| Entradas | Los latidos (vectores de gossip que llegan cada `SARD_GOSSIP_INTERVAL`, en `GossipNode.last_seen`), el registro de eventos y las `reasignacion` que llegan de otros nodos |
| Salidas | Un evento `reasignacion` (hecho) cuando toma el trabajo de un vecino caído, y los pasos pendientes hechos por el nodo (normalizado, urgencia, agrupación, triaje, ficha o replaneación). Medidas en `ResilienceStats` y en el log |
| Herramientas | Ninguna con LLM. Funciones deterministas: `Ownership.owner_of`, `pending_work`, `Node.do_work` |
| Memoria | Ninguna propia: el dueño y los pendientes salen del registro. En el proceso, solo los pasos ya intentados (cada uno se intenta una vez por proceso) |
| Límites | Solo da por caído a un vecino directo. Solo publica una reasignación si el caído tiene pendientes. Nunca edita ni borra eventos. No toca urgencias: lo que hace pasa por los mismos agentes y guardias que el dueño original (regla 1) |
| Errores | Una ronda que falla se registra en el log y se intenta en la siguiente. Un paso que falla cuenta en `failed` y no se reintenta en ese proceso. Nunca tumba el nodo, el ACK ni el gossip |
| Terminación | Cada ronda termina al recorrer los pendientes. El servicio corre mientras corre el nodo |

## Cómo decide

- **Latidos.** Un vecino del que no llega nada en `SARD_PEER_TIMEOUT` segundos (10, unas 5 rondas) está caído. Si nunca se le oyó, el tiempo cuenta desde el arranque.
- **Dueño.** El trabajo que dispara un evento es de su `origin_node`, salvo que una `reasignacion` lo cubra (`from_node` igual al origen y `seq ≤ up_to_seq`). En ese caso es de quien la publicó. Si varias lo cubren, gana el id más bajo. Se encadena: si B tomó lo de A y luego cae B, quien tome lo de B (cubriendo la reasignación de B) se queda también con lo de A. Como sale del registro, todos los nodos llegan a la misma conclusión, también C, que no oye a A, y A cuando vuelve.
- **Toma.** Un nodo que ve caído a un vecino con pendientes publica `reasignacion(from_node, up_to_seq = último consecutivo suyo conocido, silent_s)` y en la misma ronda hace esos pasos. En A ↔ B ↔ C: si cae A o C, lo toma B. Si cae B, A y C quedan partidos y lo toman los dos; al juntarse, vale A.
- **Pendiente.** Es un evento cuyo siguiente paso no está en el registro, lo haya hecho quien lo haya hecho:

| Evento | Le falta | Paso |
| :-- | :-- | :-- |
| `reporte` | `reporte_normalizado` de su `report_id` | normalizar |
| `reporte_normalizado` | `urgencia_estimada` del reporte | urgencia |
| `reporte_normalizado` | `incidente_agrupado` que lo incluya | agrupar |
| `urgencia_estimada` ambigua de N7 sin otra que la referencie | la del agente de triaje (solo si hay LLM) | triaje |
| último `incidente_agrupado` de un incidente | `ficha` del incidente | ficha |
| `decision` "rechazar" sobre la última ficha del incidente | una ficha nueva | replanear |

- **Doble procesamiento.** Antes de cada paso se revisa el registro, así que nunca se repite lo que otro ya hizo. Un triaje que termina cuando el caso ya no le toca al nodo no se publica. Si una partición hace que dos nodos procesen el mismo caso a la vez, las vistas conservan la urgencia más alta, ponen el reporte en un solo incidente y muestran la ficha paralela en los pasos.

## El nodo que vuelve

Reabre su SQLite (el consecutivo sigue desde `last_seq`) y no actúa hasta ponerse al día con un vecino, es decir, hasta que el vector del vecino no tenga nada que a él le falte. Así ve primero las reasignaciones. Si ningún vecino responde, sigue solo. Después retoma sus propios pendientes: lo que se cortó al caer, como el triaje de N8 que estaba en curso. Lo que B ya tomó no lo toca. Lo nuevo que reciba vuelve a ser suyo.

## Medición

`ResilienceStats` (`node.resilience.stats`): `recovery_s` (del arranque a ponerse al día), `takeovers` (de quién, hasta qué consecutivo, cuánto calló el vecino, cuántos pasos hizo y en cuánto tiempo), `resumed` y `failed`. En el log:

```
B: tomó el trabajo de A hasta A:12 (4 pasos en 0.01 s; A callado 10.4 s)
A: recuperado y al día en 2.1 s
```

`measure_failover` (`services/failover.py`) apaga un nodo en la red simulada, espera la toma, lo vuelve a levantar y mide todo con un reloj simulado (una ronda = 2 s). Con dos reportes de A sin procesar, semilla 3:

| Canal | Detección | Toma (hasta que C tiene todo) | Recuperación de A |
| :-- | --: | --: | --: |
| Ideal | 12 s | 14 s | 4 s |
| LoRa 30 % pérdida | 12 s | 18 s | 24 s |
| LoRa 60 % pérdida | 12 s | 76 s | 48 s |

## Limitaciones

- Con mucha pérdida, un vecino vivo puede pasar 10 s sin que le llegue nada y darse por caído. Solo hay toma si en ese momento tiene pendientes, cosa rara porque el dueño procesa al recibir. Si pasa, es el caso de la partición: los dos procesan y las vistas lo resuelven.
- Solo los vecinos directos detectan una caída. En la topología en línea basta; con más nodos haría falta propagar la vivacidad (cambio de contrato del gossip).
