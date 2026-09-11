"""Engine construction and the tenant-scoped session.

Everything that touches user data goes through :func:`tenant_session`. It opens a
transaction, tells Postgres who the request is for, and hands back a session. The
row-level security policies do the rest.

Two details are load-bearing and easy to get wrong.

**The setting is applied with ``set_config``, not ``SET LOCAL``.** ``SET LOCAL`` takes
a literal, not a bind parameter, so using it would mean formatting a user id into SQL
text. ``set_config(name, value, is_local => true)`` is exactly equivalent, is scoped to
the transaction in the same way, and takes parameters. A user id arrives from a
decoded token; it is not going into a string.

**It is applied inside the transaction, every time.** A pooled connection is reused
across requests and across users. A setting applied outside a transaction, or applied
once at checkout, survives into the next request on that connection and hands one
user's context to another. Transaction scope is what makes reuse safe.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jobtrack_core.config import Environment, Settings

#: The Postgres setting the policies read. `app.` prefix because Postgres requires
#: a qualified name for custom settings.
TENANT_SETTING: Final = "app.user_id"

_SET_TENANT = text(f"SELECT set_config('{TENANT_SETTING}', :user_id, true)")


class TenantContextError(RuntimeError):
    """Raised when a session would be opened without a tenant, or with a bad one."""


def _local_url(settings: Settings, *, user: str, password: str) -> str:
    db = settings.database
    return f"postgresql+asyncpg://{user}:{password}@{db.host}:{db.port}/{db.name}"


@dataclass(slots=True)
class Database:
    """The engine, its session factory, and whatever else has to be closed with it.

    In cloud the AlloyDB connector runs background certificate-refresh tasks that the
    engine knows nothing about. Disposing the engine without closing the connector
    leaves those tasks running and the process hangs on shutdown, so the two are
    owned together and closed together.
    """

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    _connector: Any | None = field(default=None, repr=False)

    def tenant(self, user_id: uuid.UUID) -> Any:
        """Shorthand for :func:`tenant_session` against this database."""
        return tenant_session(self.sessions, user_id)

    async def close(self) -> None:
        await self.engine.dispose()
        if self._connector is not None:
            await self._connector.close()


def create_database(settings: Settings) -> Database:
    """Build the engine and session factory for this environment.

    Local development connects over TCP to the docker compose Postgres. Cloud Run
    connects through the AlloyDB Python Connector, which handles the mTLS handshake and
    IAM database authentication, so no password exists to leak.
    """
    common: dict[str, Any] = {
        "pool_size": settings.database.pool_size,
        "max_overflow": settings.database.max_overflow,
        # Cloud Run can hold an idle instance for a long time, and a connection that
        # has been idle past the server's timeout fails on first use rather than at
        # checkout. Recycling below that window turns it into a reconnect.
        "pool_recycle": settings.database.pool_recycle_seconds,
        "pool_pre_ping": True,
        "echo": False,
    }

    if settings.environment is Environment.LOCAL:
        engine = create_async_engine(
            _local_url(
                settings,
                user=settings.database.app_user,
                password=settings.database.password.get_secret_value(),
            ),
            **common,
        )
        return Database(engine=engine, sessions=create_sessionmaker(engine))

    # Imported here rather than at module scope: the connector opens background refresh
    # tasks and expects credentials, neither of which should happen in a local test run
    # that never touches AlloyDB.
    from google.cloud.alloydb.connector import AsyncConnector, IPTypes

    connector = AsyncConnector(enable_iam_auth=True, ip_type=IPTypes.PRIVATE)

    async def connect() -> Any:
        return await connector.connect(
            settings.database.alloydb_instance_uri,
            "asyncpg",
            user=settings.database.app_user,
            db=settings.database.name,
            enable_iam_auth=True,
        )

    engine = create_async_engine("postgresql+asyncpg://", async_creator=connect, **common)
    return Database(engine=engine, sessions=create_sessionmaker(engine), _connector=connector)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory.

    ``expire_on_commit=False`` so that attributes read after a commit do not trigger a
    lazy refresh against a connection whose tenant context has already been discarded.
    """
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def tenant_session(
    factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> AsyncIterator[AsyncSession]:
    """Open a transaction scoped to one user.

    The transaction commits on a clean exit and rolls back on any exception. The tenant
    setting dies with the transaction either way, so nothing leaks onto the pooled
    connection.

    Args:
        factory: the session factory from :func:`create_sessionmaker`.
        user_id: the authenticated user. Comes from a verified token, never from a
            request body or a header the client controls.
    """
    if not isinstance(user_id, uuid.UUID):  # pragma: no cover - guards a caller mistake
        raise TenantContextError(f"user_id must be a UUID, got {type(user_id).__name__}")

    async with factory() as session, session.begin():
        await session.execute(_SET_TENANT, {"user_id": str(user_id)})
        yield session


@asynccontextmanager
async def privileged_session(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Open a transaction with no tenant set.

    For work that is genuinely not tenant-scoped: the outbox relay reading across
    users under its own role, and the global extraction cache. Every tenant policy
    compares against a setting that is absent here, which evaluates to null and
    therefore matches nothing — so this is not a way to read another user's rows, it is
    a way to touch the tables that have no owner.
    """
    async with factory() as session, session.begin():
        yield session


def create_relay_database(settings: Settings) -> Database:
    """Build an engine that connects as the relay role.

    A separate engine rather than a separate session on the shared one, because the
    role is a property of the connection. The relay's authority — reading events across
    every tenant — exists only on connections opened as `jobtrack_relay`, and that role
    holds SELECT and UPDATE on `outbox` and no grant whatsoever on any other table.
    A relay bug therefore cannot reach a resume; it gets a permission error.
    """
    common: dict[str, Any] = {
        # The relay is one process doing one query on a timer. A large pool would be
        # idle connections held against a database that charges for them.
        "pool_size": 2,
        "max_overflow": 0,
        "pool_recycle": settings.database.pool_recycle_seconds,
        "pool_pre_ping": True,
        "echo": False,
    }

    if settings.environment is Environment.LOCAL:
        engine = create_async_engine(
            _local_url(
                settings,
                user=settings.database.relay_user,
                password=settings.database.relay_password.get_secret_value(),
            ),
            **common,
        )
        return Database(engine=engine, sessions=create_sessionmaker(engine))

    from google.cloud.alloydb.connector import AsyncConnector, IPTypes

    connector = AsyncConnector(enable_iam_auth=True, ip_type=IPTypes.PRIVATE)

    async def connect() -> Any:
        return await connector.connect(
            settings.database.alloydb_instance_uri,
            "asyncpg",
            user=settings.database.relay_user,
            db=settings.database.name,
            enable_iam_auth=True,
        )

    engine = create_async_engine("postgresql+asyncpg://", async_creator=connect, **common)
    return Database(engine=engine, sessions=create_sessionmaker(engine), _connector=connector)
