# Clips de radio VHF (D7)

Clips grabados por el equipo para el prototipo (diseño §7). El guion de cada uno y lo que se espera de la separación están en `clips.json`. El nodo los usa en la barra de simulación (`POST /api/radio/demo/<id>`) y la evaluación (`python -m guadua.agents.radio_eval --clips`) mide con ellos.

## Cómo grabar

1. Lee el `guion` de cada clip en voz alta, con ritmo de radio. Mejor si se alternan dos voces; está bien con un celular o el micrófono del portátil. Graba en un lugar sin eco.
2. Dale sonido de radio VHF y guárdalo con el `id` del clip:

   ```bash
   python -m guadua.radio preparar grabacion.m4a data/radio/clip_colapso_dos_incidentes.ogg
   ```

   `preparar` usa ffmpeg: filtro de banda de radio (300–3000 Hz), compresión, ruido suave y Opus mono de 16 kHz (unos 25–40 KB por clip).
3. Revisa que Whisper lo entienda: `python -m guadua.radio transcribir data/radio/clip_colapso_dos_incidentes.ogg`.

Para grabar desde el portátil, 20 segundos:

```bash
ffmpeg -f pulse -i default -t 20 grabacion.wav
```

## Cuántos

Los 12 de `clips.json`: 4 de un solo suceso (2 directos y 2 de segunda mano), 4 de dos sucesos que mezclan directo y segunda mano, 1 de tres sucesos, 2 con jerga ("QTH", "positivo") o cortes, y 1 casi ininteligible para probar el respaldo manual del tablero. El mínimo para la demo es `clip_colapso_dos_incidentes`, que es el que usa el botón "Clip de prueba".

Solo voces del equipo, con su permiso: nada de audio real de Bomberos ni de terceros.
