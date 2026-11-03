# Build context: repository root (see docker-compose.yml / CI)
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY ingest/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ingest/app ./app
COPY tools/simulate.py ./tools/simulate.py
RUN useradd -r -u 10001 kaar
USER kaar
LABEL org.opencontainers.image.source="https://github.com/OWNER/kaar-monitoring"
CMD ["python", "-m", "app.main"]
