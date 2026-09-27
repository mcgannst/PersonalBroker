# syntax=docker/dockerfile:1
# The Trader image (SPEC §15, P4-T17): api (uvicorn), worker and cron (supercronic) under supervisord.
# Build context: the Trader/ folder (see Trader/.dockerignore: no .env file ever enters the context).
#   DOCKER_DEFAULT_PLATFORM=linux/amd64 docker --context desktop-linux build -f docker/Dockerfile -t trader:dev .
# The container runs as uid 10001 on a read-only root filesystem: only /tmp (tmpfs) and /app/logs (a volume)
# are writable, so bytecode is compiled here and HOME, the FinViz cache and supervisord's files live in /tmp.

# --- web: the React app. Its output is plain JS and CSS, so it builds on the build machine's own platform.
FROM --platform=$BUILDPLATFORM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- build: the virtual environment (locked, no dev tools, plus supervisor) and supercronic.
FROM python:3.12-slim-bookworm AS build
ARG TARGETARCH
# supercronic: the latest release when this file was written, checked against its published SHA-1 sums.
ARG SUPERCRONIC_VERSION=v0.2.49
ARG SUPERCRONIC_SHA1SUM_AMD64=e63c11a9726b775a6a11801e81af4f3fb926aa68
ARG SUPERCRONIC_SHA1SUM_ARM64=0b6c5bb743e0b0dafed1132198c81807927ac413
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN set -eu \
 && case "$TARGETARCH" in \
      amd64) sum="$SUPERCRONIC_SHA1SUM_AMD64" ;; \
      arm64) sum="$SUPERCRONIC_SHA1SUM_ARM64" ;; \
      *) echo "no supercronic checksum for architecture $TARGETARCH" >&2 && exit 1 ;; \
    esac \
 && curl -fsSL -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}" \
 && echo "${sum}  /usr/local/bin/supercronic" | sha1sum -c - \
 && chmod 0755 /usr/local/bin/supercronic
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
WORKDIR /app
# Dependencies first (a cached layer while only the code changes), then the project itself.
COPY app/pyproject.toml app/uv.lock app/.python-version ./
RUN uv sync --frozen --no-dev --group deploy --compile-bytecode --no-install-project
COPY app/alembic.ini ./
COPY app/trader ./trader
RUN uv sync --frozen --no-dev --group deploy --compile-bytecode \
 && python -m compileall -q /app/trader

# --- runtime
FROM python:3.12-slim-bookworm AS runtime
ARG APP_VERSION=dev
# Debian tzdata is for supercronic's CRON_TZ only; Python reads the pinned pip tzdata (PYTHONTZPATH empty).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 trader \
 && useradd --system --uid 10001 --gid 10001 --home-dir /tmp/home --no-create-home \
      --shell /usr/sbin/nologin trader
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONTZPATH="" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp/home \
    TRADER_FINVIZ_CACHE_DIR=/tmp/cache/trader/finviz \
    WEB_DIST_DIR=/app/web/dist \
    TZ=UTC \
    APP_VERSION=$APP_VERSION
WORKDIR /app
COPY --from=build /usr/local/bin/supercronic /usr/local/bin/supercronic
COPY --from=build /app /app
COPY --chmod=0755 docker/entrypoint.sh docker/run-worker.sh /app/docker/
COPY --chmod=0644 docker/crontab docker/supervisord.conf /app/docker/
COPY --from=web /web/dist /app/web/dist
RUN mkdir -p /app/logs && chown trader:trader /app/logs
USER trader
# Documentation only: nothing is published; NPM reaches the container over the `proxy` network.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5)"]
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["all"]
