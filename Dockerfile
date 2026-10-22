# syntax=docker/dockerfile:1
#
# Works on a plain VPS (docker build / docker compose) **and** on Railway,
# Render, Fly.io and Koyeb.
#
# Railway notes:
#   * this file deliberately contains no persistent-storage or built-in
#     container-check instructions, because Railway rejects them — attach a
#     Railway Volume at /data instead (the app picks that path up on its own)
#   * the app listens on $PORT when the platform provides one (health endpoint)
#   * everything persistent lives in MongoDB, so an ephemeral filesystem is fine
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build tools are needed by a couple of the optional dependencies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so code edits don't invalidate the wheel cache.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Writable locations. On Railway, mount a Volume at /data to make the JSON
# fallback (used only when no MongoDB is configured) survive redeploys; MongoDB
# mode needs no volume at all. Kept instruction-free on purpose for Railway.
RUN mkdir -p /app/data /app/logs /data \
    && chmod -R 777 /app/data /app/logs /data

# Plain `python main.py` in both worlds: the bot is a long-running worker.
# When PORT/HEALTH_PORT is set the app also serves a status page on it.
CMD ["python", "main.py"]
