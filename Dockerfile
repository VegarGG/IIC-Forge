# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.19@sha256:b46b03ddfcfbf8f547af7e9eaefdf8a39c8cebcba7c98858d3162bd28cf536f6

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/app/model-cache \
    HF_HUB_DISABLE_TELEMETRY=1 \
    SENTENCE_TRANSFORMERS_HOME=/app/model-cache \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

# Copy dependency metadata first so source edits do not invalidate dependency
# layers. The frozen lock is the only dependency resolution used by the image.
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY cli ./cli
COPY tradingagents ./tradingagents

RUN uv sync \
    --frozen \
    --no-dev \
    --extra production \
    --no-editable

# Triage intentionally fails fast if its production embedder is unavailable.
# Bake the pinned model revision into the image so startup never depends on
# mutable remote state or first-run network access.
RUN /app/.venv/bin/python -c \
    "from tradingagents.sensing.embeddings import SentenceTransformerEmbedder; SentenceTransformerEmbedder().load()"

FROM ${PYTHON_IMAGE} AS runtime

ARG APP_VERSION=0.2.5

LABEL org.opencontainers.image.title="IIC-Forge" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.description="Private single-operator investment intelligence"

ENV HOME=/home/appuser \
    HF_HOME=/app/model-cache \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_OFFLINE=1 \
    PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SENTENCE_TRANSFORMERS_HOME=/app/model-cache \
    TRANSFORMERS_OFFLINE=1

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
 && install -d -m 0700 -o appuser -g appuser /home/appuser/.tradingagents \
 && install -d -m 0700 -o appuser -g appuser /home/appuser/reports

WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/model-cache /app/model-cache

USER appuser
STOPSIGNAL SIGTERM
ENTRYPOINT ["iic-forge"]
