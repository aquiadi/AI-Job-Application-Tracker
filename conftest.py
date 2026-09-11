"""Test-suite-wide isolation.

Settings are read from the process environment and from a ``.env`` file in the
working directory. Both are ambient, so without this fixture a developer's own
``.env`` changes test outcomes and a test that sets an environment variable leaks
into the next one. Every test therefore starts in an empty directory with the
project's environment variables removed.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent
#: Integration tests run against a second database so a test run never destroys
#: whatever is in the development one.
TEST_DB = "jobtrack_test"
#: The compose bootstrap's fixture credentials, committed on purpose and used
#: nowhere but docker compose. The owner role is needed because the application
#: role deliberately holds no TRUNCATE.
OWNER_USER = "jobtrack_owner"
OWNER_PASSWORD = "jobtrack"  # noqa: S105 - a compose fixture, not a secret

# Prefixes owned by this project. Anything matching is cleared before each test.
_OWNED_PREFIXES: tuple[str, ...] = (
    "DB_",
    "EMBEDDING_",
    "ENVIRONMENT",
    "FIREBASE_",
    "GCS_",
    "GEMINI_",
    "GOOGLE_CLOUD_",
    "INTERNAL_INVOKER_",
    "LLM_",
    "LOG_LEVEL",
    "PUBSUB_",
    "SERVICE_NAME",
    "VERTEX_",
)


@pytest.fixture(autouse=True)
def isolated_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[pytest.MonkeyPatch]:
    """Clear project environment variables and run from a directory with no .env."""
    for name in list(os.environ):
        if name.startswith(_OWNED_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    yield monkeypatch


@pytest.fixture(scope="session")
def db_port() -> str:
    """Host port the compose Postgres is published on."""
    return os.environ.get("DB_PORT", "5433")


@pytest.fixture(scope="session")
def migrated_database(db_port: str) -> Iterator[None]:
    """Bring the test database to head once, before any integration test runs.

    Alembic is invoked as a subprocess rather than through its Python API so that the
    test exercises the same command the migrate job runs. A migration that works here
    and fails in deployment is one less thing that can happen.
    """
    env = {
        **os.environ,
        "ENVIRONMENT": "local",
        "DB_NAME": TEST_DB,
        "DB_PORT": db_port,
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


@pytest.fixture
def clean_database(migrated_database: None, db_port: str) -> Iterator[None]:
    """Empty every tenant table before the test runs.

    Integration tests share one database, and without this they only pass on a fresh
    volume: a test that creates an application for subject `idp|b1` fails on the second
    run because the first run's row is still there. Relying on unique fixture data
    instead works until it does not, and the failure looks like a product bug.

    Truncating `users` cascades through every tenant table by foreign key, which is the
    same path `DELETE /me` takes. `jd_extraction_cache` is separate because it is
    global by design and has no `user_id` to cascade from.

    Connects as the owner: the application role deliberately holds no TRUNCATE.
    """
    import asyncio

    import asyncpg

    async def truncate() -> None:
        connection = await asyncpg.connect(
            host=os.environ.get("DB_HOST", "127.0.0.1"),
            port=int(db_port),
            user=OWNER_USER,
            password=OWNER_PASSWORD,
            database=TEST_DB,
        )
        try:
            await connection.execute("TRUNCATE users, jd_extraction_cache RESTART IDENTITY CASCADE")
        finally:
            await connection.close()

    asyncio.run(truncate())
    yield
