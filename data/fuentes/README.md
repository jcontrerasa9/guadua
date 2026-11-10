# Fuentes del RAG

Documentos oficiales que consulta el RAG (tarea D8 en `docs/plan.md`). Están en el repo para que todo funcione sin Internet.

- Solo se agregan PDF en su versión final: un archivo reemplazado sigue ocupando espacio en el historial.
- Cada archivo lleva una fila en esta tabla. La versión es la que aparece en las citas de las fichas (`Citation.version`).

| Archivo | Título oficial | Entidad | Versión o fecha | Origen (URL) |
| :---- | :---- | :---- | :---- | :---- |
| `EMRE MANIZALES 2024 - Documento Principal.pdf` | Estrategia Municipal para la Respuesta a Emergencias EMRE - Documento Principal | Alcaldía de Manizales - UGR | Actualización 2024 | pendiente |
| `EMRE MANIZALES 2024 - Responsabilidades Institucionales.pdf` | Estrategia Municipal para la Respuesta a Emergencias EMRE - Responsabilidades y Funciones Institucionales | Alcaldía de Manizales - UGR | Actualización 2024 | pendiente |
| `EMRE MANIZALES 2024 - Anexo 1.pdf` | Estrategia Municipal para la Respuesta a Emergencias EMRE - Anexo 1 | Alcaldía de Manizales - UGR | Actualización 2024 | pendiente |
| `EMRE MANIZALES 2024 - Anexo 2.pdf` | Estrategia Municipal para la Respuesta a Emergencias EMRE - Anexo 2 | Alcaldía de Manizales - UGR | Actualización 2024 | pendiente |
| `EMRE MANIZALES 2024 - Anexo 3.pdf` | Estrategia Municipal para la Respuesta a Emergencias EMRE - Anexo 3 | Alcaldía de Manizales - UGR | Actualización 2024 | pendiente |
| `EMRE MANIZALES 2024 - Anexo 4.pdf` | Estrategia Municipal para la Respuesta a Emergencias EMRE - Anexo 4 | Alcaldía de Manizales - UGR | Actualización 2024 | pendiente |
| `EMRE MANIZALES 2024 - Anexo 5.pdf` | Estrategia Municipal para la Respuesta a Emergencias EMRE - Anexo 5 | Alcaldía de Manizales - UGR | Actualización 2024 | pendiente |
| `EMRE MANIZALES 2024 - Anexo 6.pdf` | Estrategia Municipal para la Respuesta a Emergencias EMRE - Anexo 6 | Alcaldía de Manizales - UGR | Actualización 2024 | pendiente |
| `EMRE MANIZALES 2024 - Suplementos para Consulta.pdf` | Estrategia Municipal para la Respuesta a Emergencias EMRE - Suplementos para Consulta | Alcaldía de Manizales - UGR | Actualización 2024 | pendiente |

## Cómo se arma el corpus del RAG

`python -m guadua.knowledge.ingest` (necesita `pdftotext`, de poppler-utils) lee estos PDF y reescribe `data/emre_passages.json`: pasajes de 60 a 180 palabras con el texto tal como está en el documento, citados por **página** (`"p. 23"`) y con la entidad o tarea de la EMRE como contexto ("19. EFIGAS SA ESP: …", "TAREA 1.2: BÚSQUEDA Y RESCATE: …"). Se descartan las páginas de matrices R/A/L y de índice.

Los resúmenes operativos que escribió el equipo (`data/rag_resumenes_equipo.json`) se agregan con `is_example=True` y el documento "Resumen operativo del equipo (no oficial)": aportan lo táctico que la EMRE no trae y el tablero los muestra como "(ejemplo)". La matriz `data/matriz_emre.csv` cita la tarea y las páginas de los Anexos funcionales.

`python -m guadua.knowledge.eval_rag` mide la búsqueda sobre páginas verificadas a mano (Hit@3 81 %) y sobre los resúmenes.
