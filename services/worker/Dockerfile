# The worker image. Identical to the api's build, different entry point.
#
# One Dockerfile per service rather than one image with two commands: they scale
# differently and are given different service accounts, and an image that can run
# either is an image with both services' permissions.

FROM python:3.12-slim-bookworm AS build

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/
COPY services/api/pyproject.toml services/api/
COPY services/worker/pyproject.toml services/worker/
COPY evals/pyproject.toml evals/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace

COPY packages/ packages/
COPY services/ services/
COPY evals/ evals/
COPY prompts/ prompts/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm AS runtime

RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY --from=build --chown=app:app /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=cloud \
    SERVICE_NAME=worker

USER app
EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn jobtrack_worker.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
