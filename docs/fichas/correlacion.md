# Agente 3 · Correlación

Código: `src/guadua/agents/correlacion.py` (agrupación) y `src/guadua/agents/correlacion_confianza.py` (confianza, rumores y contradicciones, D4). Pruebas: `tests/test_correlacion.py`, `tests/test_correlacion_confianza.py`. Diseño: §10 y §12.

| Campo | Agente 3 · Correlación |
| :---- | :---- |
| Objetivo | Que varios reportes del mismo hecho se vean como un solo incidente, con una confianza que distinga testigos directos de rumores, sin fusionar hechos distintos |
| Entradas | Cada `reporte_normalizado` del propio nodo; los grupos abiertos y sus miembros, reconstruidos del registro; las `confirmacion_campo` de rescatistas |
| Salidas | Un `incidente_agrupado` (derivado) con `report_ids`, `link` (`seguro`, `dudoso` o `nuevo`), `confidence`, `rumor` y `contradictions`. Cuando un grupo crece o llega una confirmación, un evento nuevo que reemplaza al anterior (nunca se edita) |
| Herramientas | Ninguna con LLM. Funciones deterministas: compatibilidad de tipos, distancia al centro del grupo (haversine), similitud de texto (`SequenceMatcher`), `calcular_confianza_y_rumor`, `detectar_contradicciones` |
| Memoria | Ninguna propia: los grupos se reconstruyen del registro en cada reporte. Las mismas entradas dan las mismas conclusiones en todos los nodos |
| Límites | Compara contra el **centro** del grupo, no contra el último miembro (sin efecto cadena A~B, B~C). Ventana de 2 h entre reportes y 6 h por grupo. En el mismo barrio sin GPS solo une si el tipo es muy compatible y el texto se parece (similitud ≥ 0,5); si no, deja el vínculo **dudoso** como incidente aparte. No borra originales: cada grupo conserva sus reportes |
| Errores | Un reporte ya agrupado no se vuelve a agrupar (idempotente). Un evento que no es `reporte_normalizado` se rechaza. Un fallo se registra en el log y no tumba el nodo ni el ACK |
| Terminación | Un `incidente_agrupado` publicado por reporte (o ninguno si ya estaba agrupado) |

## Cómo decide

1. **Tiempo:** si pasaron más de 2 h desde el último reporte del grupo o 6 h desde el primero, es un incidente nuevo.
2. **Tipo:** la compatibilidad debe valer con **todos** los miembros. Alta: colapso, atrapados y herido entre sí; incendio con fuga de gas. Media: incendio o gas con colapso, atrapados o herido; colapso con vía bloqueada. El resto, incompatible.
3. **Lugar:**
   - Los dos con GPS: dentro de la suma de los radios con compatibilidad alta es **seguro** (salvo textos muy distintos); en un margen de 150 m más, **dudoso**.
   - Los dos por barrio: barrios distintos son incidentes distintos; el mismo barrio es **dudoso** salvo compatibilidad alta y texto parecido.
   - Uno con GPS y otro por barrio: **dudoso**.
4. **Resultado:** el grupo seguro más cercano al centro; si solo hay dudosos, incidente aparte con `link="dudoso"`; si no, nuevo.

¿Por qué no unir todo lo del mismo barrio? En un sismo hay varios colapsos por barrio: fusionar dos direcciones escondería atrapados. Ante la duda decide una persona; el tablero le muestra los **posibles duplicados** (mismo lugar, tipo compatible, menos de 30 min) para que no despache dos veces.

## Confianza y rumores (D4)

Cada fuente pesa según quién la da (confirmación en campo 1,00; rescatista por radio que lo vio 0,85; ciudadano "lo veo" 0,60; segunda mano 0,10) y según cuánto aporta de nuevo (mismo dispositivo 0; texto casi idéntico 0,05; parecido 0,30; ubicación dudosa 0,50). Se combinan como probabilidades independientes; la segunda mano tiene un tope de 0,25. Confianza < 0,30 es baja; ≥ 0,70, alta. Sin testigos directos, es **rumor**.

Verificado: 40 copias idénticas de "me contaron" → baja y rumor; 3 testigos directos con redacción distinta → 0,936; una confirmación de campo → 1,00. Las contradicciones (p. ej., "hay atrapados" contra "ya los sacaron", o un rescatista que descarta) quedan en `contradictions` con su origen y hora.

## Pendiente

El diseño prevé que, ante un vínculo dudoso, un agente con LLM proponga unir o separar con su justificación. No está implementado: hoy esa decisión la toma el comandante con el aviso de posible duplicado.
