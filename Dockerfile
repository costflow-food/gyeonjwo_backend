# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    XDG_CONFIG_HOME=/tmp/.config \
    XDG_CACHE_HOME=/tmp/.cache
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app

# Build metadata does not invalidate the dependency-install layer.
ARG BUILD_REVISION=unknown
ENV APP_BUILD_REVISION=${BUILD_REVISION}

COPY --chown=app:app app ./app
RUN python -m app.features.review.infrastructure.pdf_worker setup /app/models/ocr \
    && chmod -R a+rX /app/models/ocr
ENV OCR_MODEL_DIR=/app/models/ocr
COPY --chown=app:app scripts ./scripts
USER 10001:10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]
# Proxy headers are trusted only from FORWARDED_ALLOW_IPS (uvicorn reads it; IPs or CIDRs).
# Default is loopback (compose + local tunnel). Lightsail sets its private LB range so
# X-Forwarded-Proto=https is honoured; never '*' on a host whose port is reachable directly.
ENV FORWARDED_ALLOW_IPS=127.0.0.1
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
