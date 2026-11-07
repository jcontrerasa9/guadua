# **SARD: Documento Maestro de Alcance, Stack, Arquitectura e Interfaz**

**Propuesta unificada — Hackathon SARD (El Gran Apagón Andino, M 7,4)** **Universidad Nacional de Colombia — Sede Manizales** · Entrega: martes 10 de noviembre, 8:30 a. m.

&nbsp;

---

## **Índice**

1. Qué une esta versión  
2. Principios de diseño  
3. Interfaz (UX de emergencia)  
4. Stack tecnológico  
5. Topología y protocolo de comunicación  
6. Captura ciudadana  
7. Entrada por radio VHF  
8. Política de transmisión  
9. Nodos, registro de eventos y sincronización  
10. Los 7 agentes  
11. Clasificación de urgencia y riesgo para rescatistas  
12. Duplicados, confianza y rumores (Reto 4\)  
13. Coordinación, RAG y recursos  
14. Repositorio, CI y secretos  
15. Ejecución, pruebas y métricas objetivo  
16. Ruta a producción  
17. Niveles de prioridad y reparto  
18. Decisiones abiertas

---

## **1\. Qué une esta versión**

La v3 junta dos aportes del equipo con lo que contó el comandante de Bomberos de Manizales en la inauguración.

**Motor distribuido y agéntico (base v2):**

* **Piso determinista \+ agencia en la zona gris.** Reglas y un clasificador local propio (scikit-learn, TF-IDF de caracteres \+ regresión logística calibrada) procesan el 100 % de los reportes sin Internet. Los agentes con LLM (LangGraph con Gemini en la nube u Ollama en local) trabajan solo en la **zona gris**: casos ambiguos, vínculos dudosos, reportes incompletos e incidentes olvidados. Nunca bajan el piso de urgencia.  
* **Patrón pizarra y nodo dueño.** Cada nodo (FastAPI \+ SQLite) guarda un registro de eventos inmutable, identificados por `(nodo_origen, consecutivo)`. No hay orquestador central: los 7 agentes se coordinan leyendo y publicando eventos. Los nodos se sincronizan por **gossip** con vectores de versión sobre la topología `A ↔ B ↔ C`, sin enlace directo A–C.  
* **Protocolo binario de 21 bytes \+ diccionario de Manizales.** Cabecera fija de 21 B más texto comprimido con un diccionario de jerga de emergencias y barrios. Cabe en un SMS (140 B) o en una trama LoRa (\~200 B). ACK con mapa de bits.

**Diferenciadores para el rescate físico:**

1. **Modo escombros a ciegas \+ baliza acústica.** Se activa sin mirar la pantalla y envía solo la cabecera mínima. Emite pulsos de sonido de unos 3000 Hz, rango en que el oído humano es más sensible, para orientar a rescatistas y binomios caninos sin que la víctima tenga que gritar.  
2. **Consideración del riesgo para rescatistas.** Los agentes de triaje y coordinación deben tener en cuenta peligros en la escena (fuga de gas, cables caídos, cilindros expuestos, colapso secundario) y advertirlos en la ficha. Apoya la función de **Seguridad** del Sistema de Comando de Incidentes que describió el comandante.  
3. **Evidencia visual diferida.** Una miniatura de la foto, reducida en el propio teléfono, viaja **al final de la cola**: nunca retrasa un reporte de texto.  
4. **Radio VHF \+ "Estoy a salvo".** Transcripción local de la radio de los socorristas y un botón mínimo para reportar estar a salvo. Se inspira en el caso de la joven atrapada que encontraron porque un registro mostró que faltaba alguien.

**Interfaz limpia:** el ciudadano ve 3 bloques grandes. El coordinador ve tarjetas despejadas, con lo vital a primera vista y el detalle técnico en un desplegable.

---

## **2\. Principios de diseño**

1. **Lo predecible lo hace el código; el juicio en la zona gris lo hacen agentes con límites; la decisión final la toma una persona.** Si el LLM falla o no está disponible, el caso pasa a revisión humana: el sistema nunca queda peor que su versión determinista.  
2. **Simplicidad bajo estrés.** El ciudadano asustado no toma decisiones técnicas, y el despachador agotado no lee muros de texto.  
3. **La seguridad del rescatista importa.** Antes de recomendar un despacho, los agentes consideran los peligros reportados en la escena y los advierten. El sistema advierte; no prohíbe ni decide.  
4. **Sin punto único de falla.** Todos los nodos ejecutan el mismo código. El tablero único se conecta al primer nodo vivo.  
5. **No se inventa nada.** Lo que no llegó queda como desconocido o aproximado, con su radio de incertidumbre en el mapa: un círculo, no un punto. Cada grupo conserva el vínculo a sus reportes originales.  
6. **Diseñar para el peor canal.** Todo reporte vital cabe en un SMS. En batería crítica o modo escombros, el mensaje es solo la cabecera de 21 B.  
7. **Cada pieza tiene dos implementaciones** (prototipo y producción) detrás de la misma interfaz.  
8. **Todo es reproducible.** Las pruebas usan semillas fijas y producen métricas verificables.

---

## **3\. Interfaz (UX de emergencia)**

Ambas pantallas aplican **divulgación progresiva**: lo vital a la vista y el detalle a un toque.

### **3.1. Ciudadano: tres bloques**

El ciudadano nunca ve términos técnicos (geohash, gossip, nodo, bytes). Fondo negro de alto contraste, que ahorra batería en pantallas OLED.

```
┌──────────────────────────────────────────────────────────┐
│  🟢 Listo sin internet                   🔋 13%  [Ayuda] │
├──────────────────────────────────────────────────────────┤
│  BLOQUE 1: SI ESTÁS ATRAPADO O A SALVO                   │
│  [ 🚨 ESTOY ATRAPADO BAJO ESCOMBROS (SOS + SONIDO)     ] │
│    (o agita el celular / toca 5 veces sin mirar)         │
│  [ ✅ ESTOY A SALVO (avisar a familia y Bomberos)       ] │
├──────────────────────────────────────────────────────────┤
│  BLOQUE 2: REPORTAR EN 3 TOQUES                          │
│  Paso 1: ¿Qué pasa?                                      │
│  [🏚️ Derrumbe] [🔥 Incendio] [⚠️ Fuga de gas]            │
│  [🩸 Herido ] [🚧 Vía cerrada] [🆘 Atrapados]             │
│  Paso 2: (●) Sí, lo veo ( ) Me contaron                  │
│          [✓] Huele a gas / cables  [✓] Alguien no respira│
│          [📷 Foto opcional]                              │
│  Paso 3: [ Barrio: El Cable ▼ ] [ Nota opcional... ]     │
├──────────────────────────────────────────────────────────┤
│  BLOQUE 3: [ 📢 PEDIR AYUDA AHORA ]                      │
│  Estado: ● Guardado ──► ○ Enviando ──► ○ Recibido        │
└──────────────────────────────────────────────────────────┘
```

* **Modo escombros:** desaparece el formulario y queda la pantalla negra con un círculo rojo que indica que la alerta salió y que el sonido está activo. Incluye un consejo breve, por ejemplo: "No enciendas fuego. Golpea un tubo o pared para que te escuchen". El texto final se validará con las recomendaciones oficiales de autoprotección.  
* **Pregunta de seguimiento del Agente 1:** aparece como una tarjeta arriba con dos botones grandes (`Sí` / `No`).

### **3.2. Comandante: tarjetas sin saturación**

Cada tarjeta de la lista corta muestra cuatro cosas a primera vista: prioridad y qué pasa, confianza, advertencia de riesgo y sugerencia. El resto va en un desplegable.

```
┌────────────────────────────────────────────────────────────────────┐
│ 🔴 CRÍTICA • COLAPSO CON 3 ATRAPADOS          📍 El Cable (GPS)     │
│ Confianza: ALTA (3 testigos directos + radio)  ⏱️ Hace 4 min        │
├────────────────────────────────────────────────────────────────────┤
│ ⚠️ RIESGO PARA RESCATISTAS: fuga de gas y cables caídos reportados. │
│    No se recomienda el ingreso de ambulancia estándar sin que       │
│    una unidad HazMat asegure la escena.                             │
├────────────────────────────────────────────────────────────────────┤
│ 🚒 Sugerencia (EMRE): Bomberos Manizales (2 unidades libres)        │
│ 💬 Qué decir: "Colapso en El Cable, 3 atrapados en primer piso,     │
│    precaución por fuga de gas."                      [🖼️ Ver foto] │
│ [🔎 Por qué está aquí / evidencia / agentes ▼] [❌ Replanear] [✅ Aprobar] │
└────────────────────────────────────────────────────────────────────┘
```

* **Debajo de la lista corta:** "+N incidentes de prioridad media o baja", desplegables. Nada se oculta, solo se ordena.  
* **Paneles:** riesgo de olvido (casos viejos sin atender), contradicciones y vínculos dudosos por resolver, solicitudes de los agentes, registro de personas a salvo, recursos por entidad (editable) y mapa con puntos para ubicaciones exactas y círculos para las aproximadas.  
* **Trazabilidad** (en el desplegable): reglas activadas, probabilidad del clasificador, fuentes y pasos de cada agente.  
* **Barra de simulación plegable para el jurado:** cambiar el perfil del canal, lanzar una ráfaga, inyectar un clip de radio, apagar el nodo A y ver los hashes de convergencia.

---

## **4\. Stack tecnológico**

Todo es **Python 3.13**, salvo la PWA ciudadana y el tablero, que son **HTML5 \+ JavaScript sin frameworks pesados** servidos por los nodos.

| Capa | Prototipo | Para qué | Por qué |
| :---- | :---- | :---- | :---- |
| Lenguaje | Python 3.13, `venv` \+ `pip`, dependencias fijadas | Nodos, agentes, clasificador, canal, radio, pruebas | Un solo lenguaje que ambos dominan |
| Captura ciudadana | PWA en HTML \+ JS, IndexedDB, Service Worker, Web Audio API, `<canvas>` | Formulario offline, baliza acústica, miniatura de foto | Pocos KB, poca batería, sin librerías externas |
| Nodo | FastAPI \+ SQLite (modo WAL) | Recibe reportes, guarda eventos, ejecuta agentes, sirve las vistas | Un archivo local por nodo, sin servidor central |
| Formato del mensaje | Cabecera binaria de 21 B (`struct` / `DataView`) \+ texto con diccionario (`zlib zdict` / `pako`) | Reporte mínimo y compresión del texto | Cabe en un SMS |
| Lotes entre nodos | MessagePack | Sincronización gossip | Binario compacto para estructuras variables |
| Canal | Simulador propio con perfiles (Wi-Fi, radio/LoRa, SMS) \+ proxy HTTP | Pérdidas, retrasos, duplicados, desorden, cortes | Reproducible, sin hardware |
| Agentes | Trabajadores asíncronos sobre la pizarra \+ ciclo de herramientas en LangGraph | Flujo desacoplado; decisiones en la zona gris con límite de pasos | Si un agente o nodo cae, el trabajo pendiente queda en el registro |
| Clasificador | scikit-learn: TF-IDF de caracteres \+ regresión logística calibrada | Urgencia base con probabilidades | Sin conexión, determinista, auditable, igual en todos los nodos |
| LLM de los agentes | Gemini (capa gratuita, modelo a confirmar) con Internet; Ollama (Qwen 2.5 o Llama 3.1) sin Internet | Razonar en la zona gris, explicar, redactar | Solo procesa la fracción dudosa, así que aguanta ráfagas y límites gratuitos |
| Embeddings y duplicados | Modelo multilingüe pequeño en ONNX (`fastembed`, sin PyTorch) \+ `rapidfuzz` | RAG semántico y similitud entre reportes | Español, sin conexión, liviano |
| RAG | SQLite FTS5 \+ embeddings en la misma SQLite (búsqueda híbrida) | Protocolos y matriz de la EMRE con citas | Sin servidor vectorial aparte |
| Transcripción de radio | `faster-whisper` (`small` o `base`, español) en CPU | Audio VHF a texto | Local y gratuito |
| Tablero y mapas | HTML \+ JS con cambio automático de nodo \+ Leaflet con mapas de Manizales en local | Lista corta, advertencias, mapa sin Internet, aprobaciones | Funciona con el Wi-Fi sin Internet |
| HTTPS en celular | `adb reverse` primero; luego `mkcert` | GPS y modo offline en el navegador | Requieren contexto seguro (HTTPS o `localhost`) |
| Ejecución y CI | Docker Compose (o script), pytest, ruff, GitHub Actions | Levantar nodos, banco de fallos, validar cada push | Un solo comando para el jurado |

**Descartadas:**

* **MySQL o PostgreSQL en el prototipo:** un servidor central contradice los nodos autónomos. En producción, PostgreSQL solo en los centros de coordinación.  
* **ChromaDB y sentence-transformers:** traen PyTorch y vuelven enorme la imagen de Docker.  
* **Clasificadores en la nube para el piso de urgencia:** sin conexión no existen y el criterio cambiaría justo cuando cae la red.  
* **MQTT como canal principal:** su broker es un punto único de falla y necesita TCP/IP, que no existe sobre SMS ni sobre tramas crudas de radio o LoRa. Queda como adaptador posible cuando hay red IP.

---

## **5\. Topología y protocolo de comunicación**

### **5.1. Quién habla con quién**

1. **Tramo corto, celular → nodo.** En la realidad, el ciudadano se conecta al Wi-Fi abierto con portal cautivo de un nodo (estación de bomberos, hospital, alcaldía) y envía el reporte por HTTP con su ID. El `200 OK` funciona como ACK. En el prototipo, este tramo pasa por un proxy que inyecta fallos.  
2. **Tramo largo, nodo ↔ nodo y flota simulada.** Simula enlaces de largo alcance degradados (radio, LoRa, SMS) con el protocolo binario completo: fragmentos, ACK con mapa de bits y presupuesto de bytes.  
3. **Propagación por saltos `A ↔ B ↔ C`.** Lo que entra por A llega a C a través de B, lo que demuestra que el sistema funciona sobre una red incompleta.

### **5.2. Formato binario del reporte**

| Campo | Tamaño | Detalle |
| :---- | :---- | :---- |
| Versión \+ tipo de mensaje | 1 B | Reporte, **SOS escombros**, fragmento, ACK, "Estoy a salvo", lote de eventos, pregunta y respuesta de seguimiento, fragmento de foto |
| ID del reporte | 8 B | 4 B de dispositivo \+ 4 B de consecutivo (un UUID en texto ocupa 36 B) |
| Hora del dispositivo | 4 B | Segundos desde una época base. Dato con incertidumbre; nunca se usa para ordenar |
| Tipo de incidente | 1 B | Atrapados, colapso, incendio, fuga de gas, herido, vía bloqueada, otro |
| Personas | 1 B | 1, 2–5, 6–20, más de 20, no sé |
| Banderas | 1 B | Bit 0: atrapados · Bit 1: no respira o sangrado grave · Bit 2: incendio · Bit 3: gas o cables caídos (riesgo en escena) · Bit 4: niños o adultos mayores · Bit 5: observación directa · Bit 6: ubicación por GPS · Bit 7: batería crítica |
| Ubicación | 5 B | Geohash de 7 caracteres (\~150 m) en 35 bits; sin GPS, código del barrio |
| Texto opcional | 0–100 B | Comprimido con diccionario. Vacío en batería crítica y en SOS escombros |
| **Total** | **21 B \+ texto** | El SOS escombros pesa 21 B. Objetivo con texto típico: caber holgado en un SMS (medir el promedio real) |

* **La baliza no usa un bit propio:** el SOS escombros es un **tipo de mensaje**, así que "baliza activa" va implícito y el bit 7 queda solo para batería crítica.  
* **Solo se comprime el texto, con diccionario.** zlib normal agrega unos 6 B de encabezado y puede agrandar un mensaje de 40 B. Un diccionario compartido con palabras frecuentes ("atrapados", "colapso", "huele a gas") y barrios y vías de Manizales (El Cable, Av. Santander, Chipre, Centro, Palermo, Milán) sí comprime frases cortas. El ahorro se mide.  
* **Fragmentos y ACK.** Si un mensaje o una foto supera la trama del perfil activo, se parte en fragmentos con el ID (8 B), su índice (1 B) y el total (1 B). El ACK lleva el ID y un mapa de bits de lo recibido; el emisor reenvía **solo lo que falta**. Una copia repetida se reconoce por su ID y no se guarda dos veces.  
* **Reintentos** con esperas que crecen exponencialmente, con un tope y algo de aleatoriedad. El tope es menor para los críticos.

#### **Contrato de la cabecera (decidido el 7 de noviembre de 2026)**

La cabecera se implementa igual en Python (`src/guadua/protocol/header.py`) y en JavaScript (`citizen/protocol/header.js`). Los vectores de `tests/fixtures/header_vectors.json` verifican que ambos producen los mismos bytes. Cambiar cualquier punto de esta lista rompe la compatibilidad entre el celular y los nodos, así que solo se cambia por decisión explícita del equipo.

| Bytes | Campo | Codificación |
| :---- | :---- | :---- |
| 0 | Versión \+ tipo | 4 bits altos: versión (hoy 1). 4 bits bajos: tipo de mensaje |
| 1–4 | Dispositivo | Entero sin signo de 32 bits |
| 5–8 | Consecutivo | Entero sin signo de 32 bits |
| 9–12 | Hora del dispositivo | Segundos desde el 1 de enero de 2026, 00:00 UTC (entero sin signo de 32 bits) |
| 13 | Tipo de incidente | 0 atrapados · 1 colapso · 2 incendio · 3 fuga de gas · 4 herido · 5 vía bloqueada · 6 otro |
| 14 | Personas | 0 una · 1 de 2 a 5 · 2 de 6 a 20 · 3 más de 20 · 4 no sé |
| 15 | Banderas | Bits 0–7 como en la tabla anterior |
| 16–20 | Ubicación | Con el bit 6 (GPS) encendido: geohash de 7 caracteres en 35 bits, seguido de 5 bits en cero. Con el bit 6 apagado: código de barrio (entero sin signo de 40 bits) |

* **Orden de bytes:** big-endian (primero el byte más significativo), el estándar en redes.
* **Tipos de mensaje:** 0 reporte · 1 SOS escombros · 2 fragmento · 3 ACK · 4 "Estoy a salvo" · 5 lote de eventos · 6 pregunta · 7 respuesta · 8 fragmento de foto.
* **Se rechaza** una cabecera con versión desconocida, con menos de 21 B, con valores fuera de las listas, con GPS sin geohash, con geohash sin GPS o con bits de relleno distintos de cero.
* **Texto opcional:** va después de los 21 B. Su compresión con diccionario, los fragmentos y el ACK se definen aparte.

### **5.3. Perfiles del canal simulado**

| Perfil | Trama | Comportamiento | Representa |
| :---- | :---- | :---- | :---- |
| Wi-Fi local | \~1200 B | Rápido, pérdidas moderadas, cortes | Celular en el portal cautivo de un nodo |
| Radio / LoRa | \~200 B | Lento, ventanas breves, pérdidas del 30 al 60 % | Enlace de largo alcance entre nodos |
| SMS | 140 B | Retrasos grandes, desorden | Red celular degradada |

Son parámetros de simulación, no mediciones de equipos reales.

---

## **6\. Captura ciudadana**

1. **Modo escombros a ciegas \+ baliza acústica.**  
   1. Pensado para alguien atrapado, a oscuras o con polvo, que no puede leer la pantalla.  
   2. Se activa con el botón superior, con 5 toques rápidos en cualquier parte o agitando el teléfono. En iPhone, detectar el movimiento requiere un permiso previo, así que se pide al abrir la app.  
   3. Envía un **SOS escombros** de 21 B con la última ubicación conocida, observación directa y batería crítica.  
   4. Emite pulsos periódicos de unos 3000 Hz para orientar a rescatistas y binomios caninos.  
   5. **En la demo (PWA):** la pantalla queda negra pero encendida, porque una página web no puede apagar la pantalla y su audio se detiene si el teléfono se bloquea.  
   6. **En la app real (nativa):** apaga la pantalla, envía el mensaje y mantiene el sonido en segundo plano.  
2. **"¿Lo estás viendo tú?"** (`Sí, lo veo` / `Me contaron`). Un bit que distingue observación directa de rumor desde el origen. Alimenta el Reto 4\.  
3. **"Estoy a salvo".** Ocupa unos 14 B, más el nombre (opcional, comprimido) y el barrio. Alimenta el registro de personas: cruza quién está bien con quién siguen buscando, reduce llamadas de familiares y enfoca la búsqueda.  
4. **Estado visible y preguntas de seguimiento.** El estado `Guardado → Enviando → Recibido` evita reenvíos por ansiedad. Si al Agente 1 le falta un dato clave, envía una pregunta corta ("¿Huele a gas?", "¿Cuántas personas?") que se responde con un toque cuando vuelve la señal.  
5. **Foto opcional.** Un `<canvas>` la reduce en el teléfono a 160×120 px en **WebP**, o **JPEG** donde el navegador no codifique WebP (Safari en iPhone). Objetivo: menos de 3 KB. Sus fragmentos viajan después de todos los textos.  
6. **Batería crítica.** Por debajo de un umbral, se envía solo la cabecera, sin texto ni foto.

---

## **7\. Entrada por radio VHF**

En el Escenario 2 (silencio digital, unas 6 horas después), el 95 % de los celulares está apagado, pero la radio de Bomberos sigue funcionando gracias a sus repetidores.

1. **Transcripción local.** Los clips de radio, con ruido y jerga, se transcriben en CPU con `faster-whisper`. En el prototipo son clips grabados por el equipo y se entregan por script.  
2. **Separación de incidentes.** Si una transmisión menciona dos sucesos ("incendio en la 23 del Centro y gente atrapada en El Cable"), el Agente 1 la divide en dos reportes ligados al mismo audio.  
3. **Directo o de segunda mano.** "Estamos en el sitio" cuenta como observación directa del rescatista (peso alto). "Nos comentan que..." cuenta como segunda mano (peso bajo).  
4. **Respaldo manual.** Campo en el tablero para que el operador escriba o corrija si el audio es ininteligible. El audio original se puede reproducir.

---

## **8\. Política de transmisión**

El Agente 4 ordena cada ventana de transmisión así:

`prioridad_efectiva = peso_urgencia + factor_envejecimiento × minutos_en_cola`

* **Pesos iniciales (se calibran en pruebas):** crítica 100, alta 70, media 40, baja 10\. Los "Estoy a salvo" y los ACK son tan pequeños que viajan junto al paquete saliente.  
* **Envejecimiento:** cada minuto en cola suma prioridad, así que ningún reporte bajo espera indefinidamente. El factor se ajusta para acotar la espera máxima, y esa espera se mide.  
* **Vencimiento sin borrado:** reportes como "vía bloqueada" sin confirmación por horas pasan a "por confirmar"; no se borran.  
* **Tamaño y adjuntos:** a igual prioridad, sale primero el más pequeño. Los fragmentos de foto tienen el peso más bajo y nunca retrasan un texto.

**Riesgo de aplazar:** un reporte bajo puede tardar más, pero no se pierde (cola local) y su espera está acotada. El riesgo real es clasificar mal un caso crítico; por eso existen las reglas de prioridad mínima.

---

## **9\. Nodos, registro de eventos y sincronización**

**Tres tipos de eventos inmutables:**

| Tipo | Ejemplos | Quién los crea | Sincronización |
| :---- | :---- | :---- | :---- |
| Hechos | Reporte, SOS, "Estoy a salvo", transcripción, confirmación en campo | Cualquier nodo que los recibe | Se replican tal cual |
| Derivados | Urgencia, explicación, duplicados, advertencia de riesgo, ficha | Solo el **nodo dueño** (el primero que recibió el reporte) | Se replican; los demás los aceptan sin recalcular |
| Decisiones humanas | Aprobar, editar, rechazar | El tablero, a través de un nodo | Si dos son incompatibles tras una partición, se marca **conflicto** y decide una persona |

**Gossip con vectores de versión.** Cada nodo guarda, por cada origen, el último consecutivo que tiene completo (por ejemplo `[A: 12, B: 8, C: 5]`). Cada pocos segundos, los vecinos que se alcanzan comparan sus vectores y se envían en lotes MessagePack solo los eventos que faltan. Como los eventos son inmutables y tienen ID único, recibir uno repetido no hace daño.

**Verificación de convergencia:** al final de una prueba, cada nodo calcula el SHA-256 de su conjunto ordenado de eventos. Si los tres hashes coinciden, los nodos convergieron sin perder ni duplicar eventos.

**Relojes:** el orden lo da el consecutivo, no la hora. La antigüedad se mide con la hora de recepción en el nodo.

**Caída y particiones:**

* Los nodos se envían latidos. Si A cae, el Agente 5 reasigna sus reportes pendientes al nodo vivo con el identificador más bajo (B). Es una regla determinista, así que todos los nodos llegan a la misma conclusión.  
* Si una partición hace que dos grupos procesen el mismo caso con conclusiones distintas, al reconectarse **se conserva la urgencia más alta** y se muestran ambas conclusiones al operador.  
* El nodo que vuelve reconstruye su estado desde su SQLite y sincroniza lo que le falta. El tiempo de recuperación se mide.  
* El tablero tiene la lista de nodos y lee del primero que responda. Si ese cae, cambia solo al siguiente e indica a cuál está conectado.

---

## **10\. Los 7 agentes**

El diseño cumple la definición clásica de sistema multiagente (agentes autónomos, reactivos, proactivos y sociales, coordinados por una pizarra sin agente central) y la moderna (un LLM que elige herramientas en un ciclo y decide cuándo terminar). El código determinista procesa todo; los agentes con LLM investigan solo la zona gris.

| \# | Agente | Piso determinista | Agencia en la zona gris | Límite |
| :---- | :---- | :---- | :---- | :---- |
| 1 | **Recepción** | Desempaqueta el binario, normaliza celular y radio, ubica el barrio en el listado offline | Separa transmisiones de radio con varios incidentes, interpreta referencias ("frente a la iglesia de Chipre"), envía 1 pregunta de seguimiento | No inventa coordenadas ni completa datos que no llegaron; máximo 1 pregunta por reporte |
| 2 | **Triaje** | Reglas de prioridad mínima \+ clasificador calibrado; marca las señales de riesgo en escena | Si el caso es ambiguo, investiga (`incidentes_cercanos`, `radio_en_zona`, `respuesta_seguimiento`) y decide subir la urgencia o marcar "verificar ya". Considera el riesgo para rescatistas al explicar | No diagnostica; **nunca baja el piso**; máximo 6 pasos |
| 3 | **Correlación** | Agrupa contra el centro del grupo; calcula la confianza | Ante un vínculo dudoso, propone unir o separar con justificación. Si llega una contradicción, reabre el grupo por iniciativa propia | No borra originales; las fusiones dudosas las confirma una persona |
| 4 | **Comunicación** | *(Servicio sin LLM)* Cola con envejecimiento, fragmentos, ACK, fotos al final | — | Ningún caso bajo espera indefinidamente |
| 5 | **Resiliencia** | *(Servicio sin LLM)* Latidos, caídas, reasignación del dueño, gossip | — | Ningún LLM decide sobre la infraestructura |
| 6 | **Coordinación** | Matriz de responsabilidades (EMRE) y tabla de recursos | Consulta protocolos (RAG); **evalúa el riesgo para rescatistas** y lo advierte en la ficha; redacta el mensaje; replanea si la persona rechaza la ficha | No llama, no despacha ni asigna recursos sin aprobación humana |
| 7 | **Vigía** | Lista de incidentes abiertos sin atender, por antigüedad | Reevalúa por iniciativa propia los casos olvidados con evidencia nueva y pide a triaje o coordinación que actúen | Solo publica solicitudes y alertas; no cambia prioridades |

**Herramientas permitidas (lista blanca):**

* **Recepción:** `buscar_lugar`, `separar_transmision`, `enviar_pregunta_seguimiento`, `marcar_campo_desconocido`  
* **Triaje:** `clasificar`, `incidentes_cercanos`, `radio_en_zona`, `respuesta_seguimiento`, `fijar_urgencia` (solo hacia arriba), `marcar_verificar_ya`, `solicitar_verificacion_campo`  
* **Correlación:** `grupos_candidatos`, `linea_de_tiempo`, `similitud_textos`, `proponer_union`, `proponer_separacion`, `registrar_contradiccion`  
* **Coordinación:** `responsable_por_tipo`, `recursos_disponibles`, `buscar_protocolo`, `evaluar_riesgo_escena`, `solicitar_revision_correlacion`, `crear_ficha`, `esperar_aprobacion`  
* **Vigía:** `incidentes_sin_atender`, `evidencia_desde`, `solicitar_reevaluacion`, `alertar_tablero`

**Límites comunes:**

* Máximo de pasos (inicialmente 6\) y de tiempo por caso; al agotarse, el caso va a revisión humana.  
* Ningún agente baja el piso de urgencia.  
* Cada paso queda en el registro y se ve en el desplegable "por qué está aquí".  
* Si el LLM no responde, devuelve algo inválido o pide una herramienta no permitida, el caso queda como "ambiguo, revisar".  
* Las únicas acciones hacia afuera son las preguntas al ciudadano y las solicitudes de verificación, y ambas se ven en el tablero.  
* **Riesgo sin Internet:** los modelos pequeños de Ollama eligen peor las herramientas. Por eso las herramientas son pocas y simples, la salida está forzada a JSON y los casos ambiguos se prueban con el modelo local.

### **Fichas de agentes (entregable 4\)**

Cada agente tendrá su ficha en `docs/fichas/` con los campos que pide el PDF: **objetivo, entradas, salidas, herramientas, memoria, límites de autonomía, manejo de errores y criterios de terminación**. Ejemplo:

| Campo | Agente 2 · Triaje |
| :---- | :---- |
| Objetivo | Que cada reporte tenga una urgencia justificada y que ningún caso posiblemente crítico quede mal clasificado sin revisión |
| Entradas | Evento "reporte normalizado"; solicitudes de reevaluación del vigía; respuestas de seguimiento |
| Salidas | Evento "urgencia estimada" (nivel, probabilidades, reglas activadas, señales de riesgo, evidencia, pasos); o "verificar ya"; o "solicitud de verificación en campo" |
| Herramientas | Las de su lista blanca |
| Memoria | Corto plazo: los pasos de la investigación en curso. Largo plazo: ninguna propia; el estado vive en el registro de eventos |
| Límites | No diagnostica; no baja el piso; máximo 6 pasos |
| Errores | Si falla el clasificador, quedan las reglas. Si falla el LLM, el caso pasa a "ambiguo, revisar" |
| Terminación | Urgencia publicada con confianza suficiente, escalamiento a revisión humana o presupuesto de pasos o de tiempo agotado |

---

## **11\. Clasificación de urgencia y riesgo para rescatistas**

Hay tres capas. Ninguna capa posterior puede bajar el piso que fija la anterior.

1. **Reglas de prioridad mínima.** Las señales vitales (atrapados, no respira, sangrado fuerte, inconsciente, incendio, SOS escombros) fijan el piso en **crítica**. Objetivo de tiempo: menos de 1 ms.  
2. **Clasificador local calibrado.** TF-IDF de n-gramas de caracteres (tolera faltas de ortografía) \+ regresión logística con `CalibratedClassifierCV`, entrenado con reportes sintéticos en español colombiano. Puede subir la urgencia, nunca bajarla. Objetivo: menos de 2 ms por reporte.  
3. **Agente de triaje (LLM) en la zona gris.** Actúa cuando la probabilidad máxima es baja (umbral inicial 0,5), cuando las dos categorías principales están muy parejas o cuando hay contradicciones. Los umbrales se ajustan con el conjunto de prueba.

**Riesgo para rescatistas (criterio, no regla).** Las señales de peligro en la escena (gas u olor a gas, cables caídos, cilindros expuestos, colapso secundario, vía bloqueada) se marcan como **señales de riesgo**. No cambian la urgencia por sí solas. Los agentes de triaje y coordinación **deben considerarlas**: la ficha muestra la advertencia en una franja visible y la recomendación tiene en cuenta el tipo de unidad adecuado (por ejemplo, HazMat antes que una ambulancia estándar). La decisión final es de la persona.

**Datos y evaluación del clasificador.**

* Reportes sintéticos con jerga y errores, generados una vez y revisados a mano en una muestra.  
* El conjunto de prueba se separa y nunca se usa para entrenar.  
* Se arma además un subconjunto de casos ambiguos para evaluar a los agentes.  
* **Métrica principal:** sensibilidad en casos críticos. El umbral favorece no dejar pasar críticos, y también se reporta la precisión.  
* **Variante si da tiempo:** embeddings \+ regresión logística, comparada con datos.  
* **Limitación declarada:** el entrenamiento es sintético.

---

## **12\. Duplicados, confianza y rumores (Reto 4\)**

**Agrupación contra el centro del grupo.** Cada reporte nuevo se compara con el centro de los grupos abiertos, no con reportes sueltos, para evitar el efecto cadena (A se parece a B, B a C, y A termina unido con C). Criterios:

* distancia ajustada al radio de incertidumbre;  
* ventana de tiempo;  
* tipos compatibles (gas e incendio en la misma cuadra sí; incendio y vía cerrada a 2 km no);  
* similitud de texto (`rapidfuzz` \+ embeddings).

Hay tres resultados posibles: vínculo seguro, vínculo dudoso (decide una persona) o incidente nuevo.

**Confianza por fuentes independientes.** La confianza crece con la **independencia** de las fuentes, no con la cantidad de mensajes.

| Fuente | Peso base `p` (inicial, se calibra) |
| :---- | :---- |
| Confirmación en campo por un rescatista | 1,00 (verificado) |
| Rescatista por radio, observación directa | 0,85 |
| Ciudadano, "sí, lo veo" | 0,60 |
| Ciudadano o radio de segunda mano | 0,10 |

Factor de independencia `ind`: 0 si viene del mismo dispositivo; 0,05 si el texto es casi idéntico a otro del grupo; 1,0 si viene de otro dispositivo, con redacción propia, observación directa y ubicación coherente.

`Confianza = 1 − Π (1 − p_i · ind_i)`

* **Objetivo:** 40 mensajes copiados y marcados "me contaron" deben quedar en confianza baja, con la etiqueta *"Posible rumor: confirmar por radio antes de mover unidades"*. Tres testigos directos independientes deben quedar en confianza alta.  
* **A probar:** muchos reportes de segunda mano con redacción distinta. Si la confianza sube demasiado, se agrega un tope para la segunda mano (ver decisiones abiertas).  
* **Urgencia frente a confianza:** "posibles atrapados" con confianza baja **nunca se oculta**. Va a la lista corta con la etiqueta **"VERIFICAR YA"**.  
* **Contradicciones:** "3 atrapados" frente a "ya los sacaron" se muestran ambas, con su fuente y su hora, como "contradicción pendiente" hasta que decida una persona.  
* **Ubicación aproximada:** un listado offline de barrios y vías de Manizales (de OpenStreetMap) convierte "cerca de la Av. Santander" en una zona con radio de incertidumbre.

---

## **13\. Coordinación, RAG y recursos**

El Agente 6 arma una ficha para que decida una persona:

1. **A quién llamar (determinista):** la matriz de responsabilidades de la **EMRE de Manizales**, por tipo de incidente. Nunca la genera el LLM. Las entidades salen de los documentos oficiales; si una no aparece ahí, se marca como ejemplo. Si no hay responsable: "sin responsable identificado, escalar al coordinador".  
2. **Con qué recursos:** la tabla editable de unidades por entidad (disponible, en misión, fuera de servicio). Si la entidad responsable no tiene unidades libres, la ficha lo advierte y sugiere escalar.  
3. **Cómo actuar y riesgos:** el RAG recupera la sección aplicable del protocolo y la **cita** (documento, sección y versión). Si hay señales de riesgo en la escena, la ficha destaca la advertencia para los rescatistas.  
4. **Qué decir:** el LLM redacta el mensaje con incidente, ubicación y su incertidumbre, personas, riesgos y confianza. Sin LLM, se usa una plantilla por tipo de incidente.  
5. **Aprobación obligatoria:** la ficha queda pendiente hasta que la persona elija **Aprobar**, **Editar** o **Rechazar y replanear**.

**RAG detrás de una interfaz (`Retriever`):**

|  | Prototipo | Producción |
| :---- | :---- | :---- |
| Búsqueda | Híbrida: FTS5 \+ embeddings en SQLite | PostgreSQL \+ pgvector en centros de coordinación; SQLite en campo |
| Documentos | EMRE, PMGRD y otros oficiales recibidos \+ ejemplos marcados como tales | Documentos oficiales versionados, con responsable por entidad |
| Calidad | \~20 preguntas con la sección esperada; se mide si aparece entre los 3 primeros resultados | Las mismas preguntas como prueba automática en cada actualización |

---

## **14\. Repositorio, CI y secretos**

```
guadua/
├── .github/          # CI (GitHub Actions) y plantilla de pull request
├── citizen/          # PWA ciudadana, baliza acústica, miniatura de foto; protocol/ con el códec JS
├── src/guadua/
│   ├── agents/       # recepción, triaje, correlación, coordinación, vigía (pizarra + LangGraph)
│   ├── services/     # comunicación y resiliencia (sin LLM), transcripción de radio
│   ├── classifier/   # reglas, clasificador, calibración y evaluación
│   ├── protocol/     # binario 21 B, diccionario, fragmentos, ACK
│   ├── channel/      # canal con perfiles y proxy de fallos
│   ├── store/        # registro de eventos, vectores de versión, gossip
│   ├── knowledge/    # matriz EMRE, recursos, RAG híbrido
│   ├── providers/    # interfaces + implementaciones (LLM, embeddings, transporte)
│   ├── node/         # aplicación FastAPI del nodo y contrato de su API HTTP
│   └── dashboard/    # tablero con cambio de nodo y mapa offline
├── data/             # datos sintéticos, clips VHF, diccionario, barrios, mapas, documentos
├── tests/            # pruebas de Python, js/ (pruebas de JS), fixtures/ (datos compartidos) y banco de fallos
├── docs/             # diseño, plan, contratos, fichas, protocolo, resultados
├── AGENTS.md         # reglas para los asistentes de IA (CLAUDE.md lo importa)
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── README.md         # instalación, reproducción y guía de carpetas
```

* **CI (GitHub Actions):** `ruff`, `pytest` y las pruebas de JS (`node --test`) en cada push y pull request. Las pruebas usan un LLM simulado y un clasificador entrenado con datos fijos, así que no dependen de ninguna API.  
* **Contrato compartido del protocolo:** pruebas que verifican que el codificador en JS y el de Python producen los mismos bytes.  
* **Ramas:** `main` guarda siempre una versión demostrable y `dev` integra el trabajo diario. Cada tarea va en una rama corta creada desde `dev` (con el ID del plan en el nombre, p. ej. `n4-sqlite-store`) y vuelve a `dev` por pull request con el CI en verde. `dev` se fusiona en `main` cuando corre de punta a punta.  
* **Colaboración:** el reparto de tareas vive en `docs/plan.md`; no se usa CODEOWNERS porque el equipo es de dos personas. La plantilla de pull request solo pide lo que el CI no verifica (contratos acordados y plan actualizado), y `pre-commit` revisa formato y bloquea `.env` antes de cada commit.  
* **Asistentes de IA:** cada integrante puede usar el asistente que prefiera; todos siguen `AGENTS.md`. La responsabilidad del código y de las decisiones es de las personas del equipo.  
* **Secretos:** la API key del LLM va en `.env`, ignorado por git. Sin ella, los agentes usan Ollama. Sin Ollama, el sistema sigue funcionando con reglas, clasificador y plantillas, y los casos dudosos van a revisión humana.  
* **Entrega:** repositorio accesible al jurado, con dependencias fijadas y un script para descargar los modelos (clasificador, embeddings, Whisper).

---

## **15\. Ejecución, pruebas y métricas objetivo**

* **Ejecución:** `docker compose up` levanta los tres nodos, el canal, el proxy y el tablero. **Funciona sin Ollama y sin API key**; Ollama se activa con un perfil de Compose. Si no hay Docker, un script lanza los mismos procesos.  
* **Datos sintéticos:** reportes con ruido, faltas de ortografía, texto incompleto, duplicados, contradicciones, rumores copiados y pegados, mezcla de observación directa y de segunda mano, y ráfagas. Además, los clips de radio grabados por el equipo.  
* **Banco de fallos con semillas fijas:** pérdida del 0, 30 y 60 %, cortes, desorden, una partición y el apagado de un nodo, en los tres perfiles del canal.

| Métrica (objetivo a medir) | Qué demuestra |
| :---- | :---- |
| Bytes por reporte: 21 B en SOS escombros y promedio con texto menor a 140 B | Que cabe en un SMS (Reto 1\) |
| Ahorro del diccionario frente a no comprimir | Que comprimir solo el texto sirve |
| Tasa de entrega con 30 y 60 % de pérdida | Que el reporte sobrevive (Reto 2\) |
| Latencia p50 y p95 por urgencia; espera máxima de los bajos | Que lo crítico sale primero y nada espera indefinidamente |
| Tiempo por reporte de reglas y clasificador (objetivos: \< 1 ms y \< 2 ms) | Que el piso aguanta ráfagas |
| Sensibilidad y precisión en críticos; matriz de confusión | Clasificador auditable |
| Duplicados bien vinculados, fusiones erróneas, rumores detectados | Reto 4 |
| Porcentaje de reportes que llegan al LLM (objetivo inicial: cerca del 15 %) | Que el LLM solo ve la zona gris |
| Casos ambiguos bien resueltos con LLM en la nube y con LLM local; pasos promedio; herramientas inválidas | Que la agencia aporta y sus límites funcionan |
| Calidad de la búsqueda del RAG | Calidad de la coordinación |
| Tiempo de recuperación de un nodo; hashes iguales tras sincronizar; tiempo de cambio de nodo del tablero | Reto 5 |
| Tamaño de la miniatura de foto (objetivo: \< 3 KB) | Que la evidencia visual es viable en un canal pobre |

Los valores reales, los errores observados y las limitaciones van al documento de resultados (entregable 6).

---

## **16\. Ruta a producción**

| Aspecto | Prototipo | Ruta a producción |
| :---- | :---- | :---- |
| Captura | PWA; `adb reverse` o `mkcert` | Portal cautivo en cada nodo; dominio propio con certificados emitidos antes del desastre y DNS local; app nativa para el modo escombros con pantalla apagada y sonido en segundo plano |
| Radio | Clips grabados \+ faster-whisper | Audio desde la consola de radio hacia un nodo; modelo ajustado con audio real; revisión humana siempre |
| Canal | Simulador con perfiles | Un adaptador por medio (radio, LoRa, Wi-Fi, SMS, satélite; MQTT si hay red IP) con la misma interfaz |
| Nodos | 3 procesos en una laptop | Equipos en centros de coordinación, hospitales y estaciones, con energía de respaldo; identidad por nodo, certificados mutuos y eventos firmados |
| Base de datos | SQLite por nodo | SQLite en campo; PostgreSQL \+ pgvector en centros de coordinación, con la misma interfaz |
| Clasificador | Datos sintéticos | Reentrenamiento periódico con reportes reales anonimizados, etiquetados por personal de las entidades |
| LLM | Gemini u Ollama, solo en la zona gris | Modelo local con buen uso de herramientas, según el hardware de cada nodo; evaluación continua con casos reales revisados |
| RAG | FTS5 \+ embeddings, documentos recibidos | Documentos oficiales versionados con responsable; pruebas de calidad automáticas |
| Matriz y recursos | CSV basados en la EMRE | Integración con los sistemas de las entidades |
| Ubicación y mapas | GPS o barrio; mapas de Manizales descargados | Integración con el SIG de la Alcaldía; servidor de mapas propio en cada nodo |
| Seguridad | Sin autenticación ni cifrado entre nodos | Autenticación de operadores, cifrado, eventos firmados, protección de datos personales según la ley colombiana |
| Escala | Cientos o miles de mensajes simulados | Pruebas de carga por etapas antes de prometer cifras |
| Aprobación humana | Botones en el tablero | Roles y permisos por entidad según el Sistema de Comando de Incidentes |
| Operación | `docker compose` local | Manual de operación, simulacros y un responsable institucional con presupuesto (la charla señaló que por ahí fallaron proyectos anteriores) |

**El prototipo no promete** cobertura real de red, rendimiento a escala de desastre, seguridad completa ni integración con los sistemas de las entidades.

---

## **17\. Niveles de prioridad y reparto**

> **Regla:** no se empieza un nivel mientras el anterior no corra de punta a punta.

| Nivel | Qué entra |
| :---- | :---- |
| **1\. Núcleo** | PWA con los 3 bloques y cola local; binario de 21 B con ACK; canal con perfiles; nodos con eventos y gossip (`A ↔ B ↔ C`); reglas \+ clasificador; correlación básica; agente de triaje con ciclo en la zona gris; agente de coordinación con matriz y aprobación humana; tablero con tarjetas, cambio de nodo y mapa offline; caída y recuperación de un nodo; banco de fallos con métricas |
| **2\. Diferenciadores** | Modo escombros \+ baliza acústica; "Estoy a salvo" y registro de personas; consideración del riesgo para rescatistas en triaje y coordinación; confianza y rumores a fondo con correlación agéntica; preguntas de seguimiento; vigía; radio VHF; RAG y recursos; barra de simulación para el jurado |
| **3\. Extras** | Miniatura de foto; variante del clasificador con embeddings; detección por agitar el teléfono; mula de datos entre particiones; portal cautivo simulado; varias entidades con índices propios |

**Reparto (detalle por tarea en `docs/plan.md`):**

* **Jacobo (backend):** registro de eventos, nodo dueño y gossip; protocolo binario, diccionario, canal y cola; reglas, clasificador, agente de triaje (ciclo en LangGraph) y script de métricas.  
* **Oscar (interfaces, campo y coordinación):** PWA de 3 bloques con modo escombros, baliza, "Estoy a salvo", "¿lo estás viendo?" y foto; codificador binario en JS; tablero con tarjetas, advertencia de riesgo, desplegable de evidencia, cambio de nodo, mapa offline, registro de personas y barra de simulación; agentes de correlación y coordinación; radio con faster-whisper; `adb reverse` y `mkcert` en el celular.

---

## **18\. Decisiones abiertas**

* \[x\] **Equilibrar el reparto:** correlación, coordinación, codificador JS, registro de personas y barra de simulación pasan a Oscar. **Queda por asignar** (al terminar el nivel 1): agente de recepción, vigía, RAG y recursos.  
* \[ \] **Tope para reportes de segunda mano** en la fórmula de confianza, si las pruebas muestran que muchos rumores redactados distinto suben demasiado la confianza.  
* \[ \] **Documentos oficiales** (EMRE, matriz de responsabilidades, PMGRD y otros): solicitados al organizador. Al recibirlos, decidir qué entra al RAG y qué entidades son reales.  
* \[ \] **Modelo de Gemini** disponible en la capa gratuita al momento de construir.  
* \[ \] **Modelo de Ollama** para los agentes sin Internet (pequeño, con buen uso de herramientas, aceptable en CPU), elegido con la prueba de casos ambiguos.  
* \[ \] **Modelo de embeddings** concreto (multilingüe, pequeño, en ONNX para `fastembed`).  
* \[ \] **Texto del consejo de autoprotección** del modo escombros, validado con recomendaciones oficiales.  
* \[ \] **Docker** disponible en ambos equipos.  
* \[ \] **Criterios de evaluación** y pesos del jurado, si la organización los publica.

&nbsp;
