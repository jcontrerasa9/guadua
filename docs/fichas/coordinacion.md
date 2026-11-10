# Agente 6 · Coordinación

Código: `src/guadua/agents/coordinacion.py`, con `src/guadua/knowledge/` (matriz EMRE, recursos y RAG). Pruebas: `tests/test_coordinacion.py`, `tests/test_rag.py`. Diseño: §10 y §13.

| Campo | Agente 6 · Coordinación |
| :---- | :---- |
| Objetivo | Que cada incidente tenga una ficha lista para que una persona apruebe: quién responde, con qué recursos, qué riesgos hay en la escena y qué decirle a la unidad |
| Entradas | El `incidente_agrupado` (y sus reportes normalizados), las fichas y decisiones anteriores del incidente, la matriz EMRE (`data/matriz_emre.csv`), la tabla de recursos (`data/recursos_ejemplo.csv`), el catálogo de riesgos (`data/riesgos_unidad.csv`) y el RAG (`data/emre_passages.json`) |
| Salidas | Un evento `ficha` (derivado, pendiente de decisión humana): `responsible`, `units_available`, `risk_warning`, `message`, `citations` y `from_template` |
| Herramientas | Deterministas: `responsable_por_tipo`, `recursos_disponibles`, `cita_por_tipo`, `Retriever.search` (RAG híbrido FTS5 + TF-IDF). LLM (cadena Gemini → Ollama): solo redacta el mensaje |
| Memoria | Ninguna propia: el estado (fichas, decisiones, unidades comprometidas) sale del registro |
| Límites | El responsable lo decide la matriz EMRE, nunca el LLM. No llama, no despacha ni asigna recursos: la ficha queda pendiente hasta **Aprobar**, **Editar** o **Rechazar y replanear**. Un incidente con ficha pendiente o aprobada no recibe otra. El mensaje del LLM no puede omitir el lugar, la falta de unidades, la unidad especializada ante riesgos ni la replanificación (las agrega el código si faltan); "verificar al llegar" lo pone el código solo con rumor o confianza < 50 %; las citas no van en el texto |
| Errores | Sin LLM, si falla o responde un mensaje inutilizable: plantilla determinista (`from_template=True`). Si falla el RAG: solo la cita de la matriz. Si mientras el LLM redactaba apareció otra ficha del incidente, no se publica la tardía. Nunca tumba el nodo ni el ACK |
| Terminación | Una ficha publicada por incidente (o ninguna si ya tenía una vigente). Tras un rechazo, una ficha nueva excluyendo las entidades rechazadas |

## Pasos

1. **Preparar (determinista, en el hilo del nodo).** Tipo principal del grupo (atrapados > colapso > incendio > gas > herido > vía > otro); responsable de la EMRE sin las entidades ya rechazadas; unidades libres = las de la tabla menos las comprometidas en despachos aprobados o editados de esa entidad; advertencias de riesgo y unidad especializada del catálogo (p. ej., gas → HazMat); y pasajes del RAG con una consulta que combina el tipo, las señales de riesgo y lo que escribieron los ciudadanos.
2. **Redactar (LLM, en otro hilo).** Recibe el caso en JSON: tipo, lugar e incertidumbre, personas, señales vitales, textos de los ciudadanos, confianza, rumor, entidad, unidades, riesgo, recomendación y el protocolo aplicable. Devuelve `{"final": {"message": "..."}}` de máximo ~450 caracteres. Las guardias completan lo obligatorio y limpian citas.
3. **Publicar (en el hilo del nodo).** Revisa que la ficha siga haciendo falta y la guarda con sus citas.

## Citas

- **Quién responde:** la tarea de la EMRE en los Anexos funcionales, con páginas reales (p. ej., "EMRE Manizales 2024 - Anexo 1 · Tarea 1.2, p. 6-8" para búsqueda y rescate).
- **Protocolo:** el mejor pasaje **oficial** de los PDF (documento y página) y el mejor **resumen del equipo**, marcado como ejemplo porque no es norma oficial.

## Ejemplo (Ollama `qwen2.5:7b`)

> **Entrada:** "Creo que mi vecino tiene una fuga de gas o tal vez su basura huele muy mal", El Cable.
> **Ficha:** Cuerpo Oficial de Bomberos (4 unidades libres); riesgo: fuga de gas, explosión o intoxicación.
> **Mensaje:** "Unidad Bomberos, El Cable. Fuga de gas reportada. Riesgo de explosión. Activa protocolo HazMat. Evacúen en dirección contraria al viento."
