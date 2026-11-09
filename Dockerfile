# Arma la imagen de los nodos y del proxy: una sola; los diferencia el entorno (node/config.py).
# Construirla la primera vez necesita Internet (Python y dependencias); correrla, no.

FROM python:3.13-slim

WORKDIR /app
# 1) Dependencias, en su propia capa: solo se reinstalan si cambia pyproject.toml, no con
#    cada cambio de código. La caché de pip sobrevive entre builds (BuildKit).
#    Extra ml: el clasificador de urgencia (N7). Se entrena al arrancar con data/sinteticos.
#    hatchling y editables permiten instalar el código abajo sin descargar nada más.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb')); \
print('\n'.join(p['build-system']['requires'] + p['project']['dependencies'] \
+ p['project']['optional-dependencies']['ml']))" > /tmp/requirements.txt \
    && echo "editables==0.6" >> /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt
# 2) El código. Instalación editable: el código encuentra data/ y citizen/ junto a src/,
#    como en desarrollo. Sin dependencias ni descargas: ya están en la capa anterior.
COPY README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps --no-build-isolation -e .
COPY data/barrios.json data/diccionario_manizales.txt data/matriz_emre.csv data/recursos_ejemplo.csv ./data/
COPY data/sinteticos/reportes.csv ./data/sinteticos/
COPY citizen ./citizen

ENV SARD_HTTP_HOST=0.0.0.0 \
    SARD_DB=/var/lib/sard/eventos.sqlite
VOLUME /var/lib/sard
EXPOSE 8000/tcp 9000/udp

CMD ["python", "-m", "guadua"]
