# Agente 4 · Comunicación

Código: `src/guadua/services/queue.py` (cola del nodo, N11), `src/guadua/protocol/fragments.py` (fragmentos y ACK, N2) y `citizen/queue.js` (cola del celular, N13). Pruebas: `tests/test_queue.py`, `tests/test_fragments.py`, `tests/js/citizen_queue.test.mjs`. Diseño: §5 y §8. Es un servicio **sin LLM**. El protocolo completo está en [../protocolo.md](../protocolo.md).

| Campo | Agente 4 · Comunicación |
| :---- | :---- |
| Objetivo | Que todo mensaje llegue aunque el canal pierda tramas, que lo crítico salga primero y que nada espere indefinidamente |
| Entradas | Mensajes binarios por enviar (reportes, SOS, "Estoy a salvo", respuestas, eventos de gossip) con su urgencia; los ACK que llegan; el perfil del canal (MTU, pérdida, retrasos) |
| Salidas | Tramas en el canal (`Transport`), fragmentadas si superan la MTU; el estado de cada mensaje (`encolado`, `transmitiendo`, `entregado`, `por_confirmar` en el nodo; `guardado`, `enviando`, `recibido`, `rechazado` en el celular) |
| Herramientas | Deterministas: prioridad con envejecimiento, `split` / `encode_fragment`, `Ack` con mapa de bits, espera creciente entre reintentos |
| Memoria | La cola misma: en el nodo, en memoria; en el celular, en IndexedDB (sobrevive a cerrar la app y a quedarse sin batería) |
| Límites | No decide urgencias: usa la que viene del triaje. No borra un mensaje sin confirmar: tras 2 h pasa a `por_confirmar`. Las fotos nunca retrasan un texto |
| Errores | Sin ACK, reintenta con espera creciente (nodo: 2 s, doblando hasta 16 s; celular: 1 s hasta 30 s). Un fragmento perdido se reenvía solo, sin repetir los demás. En el celular, un **400** del nodo (mensaje inválido) es estado final y avisa al ciudadano; un 5xx o la falta de red se reintentan |
| Terminación | Un mensaje termina cuando llega su ACK (o un `200` con `duplicate: true`) o, en el celular, cuando el nodo lo rechaza |

## Prioridad

```
prioridad_efectiva = peso_urgencia + 3 × minutos_en_cola
```

| Tipo | Peso |
| :-- | --: |
| Control: ACK, SOS de escombros, "Estoy a salvo" | 110 |
| Crítica / Alta / Media / Baja | 100 / 70 / 40 / 10 |
| Fragmentos de foto | 5 |

A igual prioridad sale primero el mensaje más pequeño. El envejecimiento hace que un reporte bajo supere a uno alto nuevo tras unos 20 minutos: nadie espera para siempre.

## Medido (banco de fallos, semilla 15)

Ráfaga de 60 reportes: **60/60 entregados** en los 12 escenarios (Wi-Fi, LoRa y SMS con 0, 30 y 60 % de pérdida y cortes). Con LoRa y 30 % de pérdida, lo crítico llega en 19 s (p50) y lo bajo en 126 s, con 2,4 tramas por reporte. Ver [../resultados.md](../resultados.md).
