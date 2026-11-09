"""Banco de fallos con métricas (N15, docs/diseno.md §15).

    python -m guadua.bench                      # tabla completa, semilla por defecto
    python -m guadua.bench --semilla 7 --salida resultados.md
    python -m guadua.bench --rapido             # solo LoRa 0 y 60 %, menos reportes

Todo corre sobre `SimulatedNetwork` con un reloj simulado: sin red real, Docker, Internet ni
API key. Con la misma semilla sale la misma tabla, salvo la fila de tiempos de CPU, que se
marca. El banco mide; no cambia el nodo, los agentes ni los contratos.
"""
