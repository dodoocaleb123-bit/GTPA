FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend backend
COPY config config
COPY frontend frontend
ENV PYTHONPATH=/app/backend GPTA_DB=/data/gpta.db
# Render injects $PORT; default 8000 for local docker. Exactly ONE worker: the scheduler runs in-process.
CMD ["sh", "-c", "mkdir -p $(dirname $GPTA_DB) && exec uvicorn app.main:app_factory --factory --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
