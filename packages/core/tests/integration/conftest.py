"""Fixtures for tests that need a real Postgres.

These run against `jobtrack_test`, a second database created by the compose init SQL,
so a test run never destroys whatever is in the development database.

Migrations are applied by invoking Alembic as a subprocess rather than through its
Python API. That is deliberate: it exercises the same command a deploy runs, so a
migration that works in the test suite and fails in the migrate job is one less thing
that can happen.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

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

REPO_ROOT = Path(__file__).resolve().parents[4]
TEST_DB = "jobtrack_test"


def _port() -> str:
    return os.environ.get("DB_PORT", "5433")


def _url(user: str, password: str) -> str:
    return f"postgresql+asyncpg://{user}:{password}@127.0.0.1:{_port()}/{TEST_DB}"


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[None]:
    """Bring `jobtrack_test` to head before any integration test runs."""
    env = {
        **os.environ,
        "ENVIRONMENT": "local",
        "DB_NAME": TEST_DB,
        "DB_PORT": _port(),
        "PYTHONPATH": ":".join(
            str(REPO_ROOT / p)
            for p in (
                "packages/core/src",
                "services/api/src",
                "services/worker/src",
                "evals/src",
            )
        ),
    }
    result = subprocess.run(
        ["uv", "run", "alembic", "-c", "packages/core/alembic.ini", "upgrade", "head"],  # noqa: S607
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")
    yield


@pytest_asyncio.fixture
async def app_engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """An engine connected as the *application* role.

    Every row-level security assertion in this suite depends on this connection not
    being the table owner and not being a superuser. Postgres does not enforce a policy
    against a superuser at all, and enforces it against an owner only because the
    tables set FORCE. Connecting as anything else here would make the tests pass for
    the wrong reason.
    """
    # NullPool: each test gets fresh connections and nothing is held open between
    # them, which keeps a pooled connection from outliving the event loop it was
    # created on and surfacing as an unclosed-socket warning at teardown.
    engine = create_async_engine(_url("jobtrack_app", "jobtrack"), poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def owner_engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """An engine connected as the schema owner, for setup and teardown only."""
    engine = create_async_engine(_url("jobtrack_owner", "jobtrack"), poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def sessions(app_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(app_engine)
