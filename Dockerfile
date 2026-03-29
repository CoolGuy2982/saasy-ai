# Dockerfile
FROM python:3.12-slim

# Logs appear immediately
ENV PYTHONUNBUFFERED=1

ENV APP_HOME=/app
WORKDIR $APP_HOME

# Only copy what's needed (venv is rebuilt inside the image)
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . ./

# Cloud Run sets $PORT (default 8080).
# 1 worker = all background threads share one process → _active_builds stays consistent.
# --timeout 0 = gunicorn never kills long-running WebSocket workers.
# --keep-alive 75 = keep idle connections alive through Cloud Run's 75s proxy timeout.
CMD exec gunicorn \
    --bind "0.0.0.0:${PORT:-8080}" \
    --workers 1 \
    --worker-class uvicorn.workers.UvicornWorker \
    --timeout 0 \
    --keep-alive 75 \
    --log-level info \
    app:app
