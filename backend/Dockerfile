# Backend image. Used by Render, and by `docker compose up` for a local
# one-command demo.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user. Nothing here needs to write to the image.
RUN useradd --create-home --uid 10001 ledger && chown -R ledger:ledger /app
USER ledger

EXPOSE 8000

# Migrations run on boot, which is the right trade-off for a single-instance
# demo. With multiple replicas this belongs in a release step instead, so two
# instances cannot race to apply the same migration.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
