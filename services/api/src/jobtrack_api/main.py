"""The public HTTP API.

Routers stay thin: they parse the request, call the domain layer, and shape the
response. Business rules live in ``jobtrack_core.domain`` where they can be tested
without a web server, and every Google Cloud dependency sits behind an interface so the
same domain code runs against the local stack.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Final

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from jobtrack_api.errors import install_error_handlers
from jobtrack_api.routers import me
from jobtrack_core import __version__
from jobtrack_core.auth import FirebaseTokenVerifier
from jobtrack_core.config import Settings, get_settings
from jobtrack_core.db.session import create_database
from jobtrack_core.logs import configure_logging, get_logger

logger = get_logger(__name__)

SERVICE: Final = "api"


class Health(BaseModel):
    """Liveness response. Deliberately says nothing about dependencies."""

    service: str
    version: str
    environment: str


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(settings)

    app.state.settings = settings
    app.state.database = create_database(settings)
    app.state.verifier = FirebaseTokenVerifier(
        settings.project_id,
        emulator_host=settings.auth_emulator_host or None,
    )

    logger.info(
        "service_starting",
        service=SERVICE,
        version=__version__,
        environment=settings.environment.value,
        region=settings.region,
        auth_emulator=bool(settings.auth_emulator_host),
    )
    try:
        yield
    finally:
        await app.state.database.close()
        logger.info("service_stopping", service=SERVICE)


def _operation_id(route: APIRoute) -> str:
    """Readable, stable operation ids for the generated TypeScript client.

    The default is derived from the path and method, producing names like
    `read_me_me_get` that someone then has to read in application code.
    """
    return f"{route.tags[0]}_{route.name}" if route.tags else route.name


app = FastAPI(
    title="Job Application Tracker API",
    version=__version__,
    lifespan=lifespan,
    generate_unique_id_function=_operation_id,
)

install_error_handlers(app)
app.include_router(me.router)


@app.get("/healthz", tags=["health"], summary="Liveness check")
async def healthz() -> Health:
    """Report that the process is up.

    This never touches the database. A liveness probe that depends on a downstream
    service turns one slow dependency into a restart loop across every instance.
    """
    settings: Settings = get_settings()
    return Health(
        service=SERVICE,
        version=__version__,
        environment=settings.environment.value,
    )
