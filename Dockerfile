# Arma la imagen de cada nodo. Todos son iguales; los diferencia el entorno (node/config.py).
# Construirla la primera vez necesita Internet (Python y dependencias); correrla, no.

FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# Instalación editable: el código encuentra data/ y citizen/ junto a src/, como en desarrollo.
RUN pip install --no-cache-dir -e .
COPY data/barrios.json data/diccionario_manizales.txt data/matriz_emre.csv data/recursos_ejemplo.csv ./data/
COPY citizen ./citizen

ENV SARD_HTTP_HOST=0.0.0.0 \
    SARD_DB=/var/lib/sard/eventos.sqlite
VOLUME /var/lib/sard
EXPOSE 8000/tcp 9000/udp

CMD ["python", "-m", "guadua"]
