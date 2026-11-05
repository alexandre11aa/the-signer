FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Unprivileged user; the filesystem is also mounted read-only by docker-compose.
RUN useradd --system --no-create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# --no-access-log: request lines are not logged. Log level warning keeps logs minimal.
CMD ["uvicorn", "app.core.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--log-level", "warning", "--no-server-header"]
