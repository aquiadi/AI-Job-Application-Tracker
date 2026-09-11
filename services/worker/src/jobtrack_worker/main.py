"""Event and task handlers.

The worker is a separate Cloud Run service rather than a background thread in the
api, for two reasons. Its endpoints are called by Pub/Sub push and Cloud Tasks with
service-account OIDC tokens and are never reachable by a user, so they get their own
identity and their own IAM. And its work is slow and bursty — extraction, embedding,
generation — so it needs to scale on a different curve from request serving.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Final

from fastapi import FastAPI
from pydantic import BaseModel

from jobtrack_core import __version__
from jobtrack_core.config import Settings, get_settings
from jobtrack_core.logs import configure_logging, get_logger

logger = get_logger(__name__)

SERVICE: Final = "worker"


class Health(BaseModel):
    """Liveness response. Deliberately says nothing about dependencies."""

    service: str
    version: str
    environment: str


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(settings)
    logger.info(
        "service_starting",
        service=SERVICE,
        version=__version__,
        environment=settings.environment.value,
        region=settings.region,
    )
    app.state.settings = settings
    yield
    logger.info("service_stopping", service=SERVICE)


app = FastAPI(
    title="Job Application Tracker worker",
    version=__version__,
    lifespan=lifespan,
    # Nothing here is part of the public contract, and publishing a schema for
    # endpoints only Pub/Sub and Cloud Tasks may call invites confusion.
    openapi_url=None,
)


@app.get("/healthz", tags=["health"], summary="Liveness check")
async def healthz() -> Health:
    """Report that the process is up, without touching any dependency."""
    settings: Settings = get_settings()
    return Health(
        service=SERVICE,
        version=__version__,
        environment=settings.environment.value,
    )
