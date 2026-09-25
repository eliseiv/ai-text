# syntax=docker/dockerfile:1.7
# Multi-stage image for the service core.
# Base: official python:3.12-slim-bookworm, pinned by digest for reproducibility.
# Runtime runs as non-root. No secrets are baked in — all config via env.

# --- Stage 1: builder -------------------------------------------------------
# Digest pin (update via `docker buildx imagetools inspect python:3.12-slim-bookworm`).
# VERIFIED: this digest resolves to CPython 3.12.13
#   (`docker run --rm python:3.12-slim-bookworm@sha256:93ab4b... python --version`),
# which satisfies the required Python 3.12.x. A tag name is NOT proof of the
# runtime version behind a digest — re-run that check whenever the digest is bumped.
FROM python:3.12-slim-bookworm@sha256:93ab4b7fa528b25124c97bcc755415e60eb671a86b4dbe0328df2fe2d1c1193d AS builder

# uv 0.4.30 (CI pin), copied from the official uv image (pinned).
COPY --from=ghcr.io/astral-sh/uv:0.4.30 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

# C toolchain for native deps without prebuilt wheels. Builder-only: never reaches runtime.
# No exact apt patch-pin — Debian security updates drop specific patch versions and would break
# reproducible builds; the base image digest already pins the apt snapshot.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first (cached layer) from the locked manifest only.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Then the sources, and install the project itself (no dev deps in the runtime image).
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- Stage 2: runtime -------------------------------------------------------
# Same digest as builder (verified CPython 3.12.13).
FROM python:3.12-slim-bookworm@sha256:93ab4b7fa528b25124c97bcc755415e60eb671a86b4dbe0328df2fe2d1c1193d AS runtime

# curl is required by the container HEALTHCHECK. No exact apt patch-pin (see builder note).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (minimal attack surface).
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --from=builder --chown=10001:10001 /app/src /app/src
COPY --from=builder --chown=10001:10001 /app/migrations /app/migrations
COPY --from=builder --chown=10001:10001 /app/alembic.ini /app/alembic.ini

# Mount point of the originals volume (PDF / text / web snapshots), shared by api and worker.
RUN mkdir -p /data/files && chown 10001:10001 /data/files

USER 10001:10001

EXPOSE 8000

# Liveness probe at container level (GET /health).
# Compose overrides it with the app-level readiness probe (GET /ready: db + redis).
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Prod process manager: Gunicorn + UvicornWorker.
# Graceful shutdown: gunicorn handles SIGTERM and drains workers within --graceful-timeout.
CMD ["gunicorn", "app.main:app", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "-w", "4", \
     "-b", "0.0.0.0:8000", \
     "--graceful-timeout", "30", \
     "--timeout", "90", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
