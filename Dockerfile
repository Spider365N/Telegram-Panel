# syntax=docker/dockerfile:1
# VPS deployment image. The app runs as an unprivileged user; the small entry
# script starts as root only long enough to prepare persistent volumes and copy
# the read-only host config into /run, then drops privileges with gosu.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/telegram

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 telegram \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin telegram

# Keep dependency installation as a separate layer for faster rebuilds.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN mkdir -p /app/data/sessions /app/logs /data \
    && chown -R telegram:telegram /app/data /app/logs /data /home/telegram \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh

EXPOSE 8070
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "main.py"]
