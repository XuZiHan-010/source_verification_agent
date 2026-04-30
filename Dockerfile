FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml pyproject.toml
COPY src/ src/
COPY config/ config/
COPY web/ web/
COPY start.sh start.sh

RUN pip install --no-cache-dir -e .

EXPOSE 8000

CMD ["bash", "start.sh"]
