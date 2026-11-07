# Arma la imagen de cada nodo. Todos son iguales.

FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

CMD ["python", "-m", "guadua"]
