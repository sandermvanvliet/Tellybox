FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-nld tesseract-ocr-deu \
 && rm -rf /var/lib/apt/lists/* \
 # Default uid/gid 1500, unlikely to clash with accounts on the host. The entrypoint
 # chowns the data folders and drops to PUID/PGID (default 1500) at start (DP-3).
 && groupadd --gid 1500 tellybox \
 && useradd --uid 1500 --gid tellybox --create-home --shell /usr/sbin/nologin tellybox

WORKDIR /app
COPY pyproject.toml ./
COPY tellybox ./tellybox
RUN pip install --no-cache-dir .
COPY scripts ./scripts

ENV PYTHONUNBUFFERED=1 \
    TELLYBOX_SCRIPTS_DIR=/app/scripts \
    TELLYBOX_DATA_DIR=/data \
    TELLYBOX_MEDIA_DIR=/media \
    XDG_CACHE_HOME=/tmp/.cache

# Kept late so a new APP_VERSION (varies every build) doesn't bust the pip layer's cache.
ARG APP_VERSION=dev
ENV TELLYBOX_VERSION=$APP_VERSION

# Starts as root only to fix folder ownership, then drops to PUID/PGID (tellybox/entrypoint.py).
ENTRYPOINT ["python", "-m", "tellybox.entrypoint"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-m", "tellybox.healthcheck"]

# One image, several services (web, cast, worker); compose picks the command.
CMD ["python", "-m", "tellybox.web"]
