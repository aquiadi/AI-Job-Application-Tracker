"""Engines for the row-level security tests.

The shared `migrated_database` and `db_port` fixtures live in the repository-root
conftest, because the api integration tests need them too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jobtrack_core.db.session import create_sessionmaker

TEST_DB = "jobtrack_test"


def _url(user: str, password: str, port: str) -> str:
    return f"postgresql+asyncpg://{user}:{password}@127.0.0.1:{port}/{TEST_DB}"


@pytest_asyncio.fixture
async def app_engine(migrated_database: None, db_port: str) -> AsyncIterator[AsyncEngine]:
    """An engine connected as the *application* role.

    Every assertion in this suite depends on this connection not being the table owner
    and not being a superuser. Postgres does not enforce a policy against a superuser
    at all, and enforces it against an owner only because the tables set FORCE.
    Connecting as anything else would make the tests pass for the wrong reason.
    """
    # NullPool: nothing is held open between tests, so a pooled connection cannot
    # outlive the event loop that created it and surface as an unclosed socket.
    engine = create_async_engine(_url("jobtrack_app", "jobtrack", db_port), poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def owner_engine(migrated_database: None, db_port: str) -> AsyncIterator[AsyncEngine]:
    """An engine connected as the schema owner, for setup and teardown only."""
    engine = create_async_engine(_url("jobtrack_owner", "jobtrack", db_port), poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def sessions(app_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(app_engine)
