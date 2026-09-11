# The api image. Built from the repository root so the workspace members resolve.
#
# Two stages. The first installs dependencies into a virtualenv with uv and the
# lockfile; the second copies that venv into a slim runtime with no build tools and no
# uv cache. The result is what runs on Cloud Run, so it is also what a cold start pays
# for.

FROM python:3.12-slim-bookworm AS build

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Manifests first, so a dependency install is cached across source-only changes. The
# member pyproject files are needed because the workspace resolves against them.
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

# Runs as a non-root user. Cloud Run does not require it; a container that cannot
# write outside its own tree is a smaller problem when something goes wrong.
RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY --from=build --chown=app:app /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=cloud \
    SERVICE_NAME=api

USER app
EXPOSE 8080

# Cloud Run sets PORT and it is not always 8080. Honouring it is the difference
# between a service that starts and one that fails its health check for ten minutes.
CMD ["sh", "-c", "exec uvicorn jobtrack_api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
