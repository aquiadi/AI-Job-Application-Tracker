"""Alembic environment.

Migrations run as the schema owner. Nothing else in the system connects as that role:
the owner is the only principal that can create or alter tables, and row-level
security is only fully enforced against non-owners, so keeping the two apart is what
makes the policies mean anything at runtime.

The engine is async because asyncpg is the only Postgres driver this project installs,
and installing a second one purely for migrations is a dependency and a second set of
connection semantics for no benefit.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from jobtrack_core.config import Environment, get_settings
from jobtrack_core.db.base import Base
from jobtrack_core.db.models import (  # noqa: F401 - imported so the metadata is populated
    Application,
    Artifact,
    JdExtractionCache,
    Job,
    JobRequirement,
    LlmCall,
    Nudge,
    Outbox,
    Profile,
    ProfileItem,
    StageEvent,
    User,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _owner_url() -> str:
    """Connection URL for the schema owner."""
    settings = get_settings()
    db = settings.database

    if settings.environment is Environment.LOCAL:
        return (
            f"postgresql+asyncpg://{db.owner_user}:{db.owner_password.get_secret_value()}"
            f"@{db.host}:{db.port}/{db.name}"
        )

    # In cloud the migrate job authenticates to AlloyDB as an IAM principal through
    # the connector, so there is no URL to build and no password to hold. The job
    # supplies the connection itself.
    raise RuntimeError(
        "ENVIRONMENT=cloud migrations run through the AlloyDB connector; "
        "invoke them with scripts/migrate.sh rather than a URL"
    )


def _include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Keep autogenerate away from things it does not manage.

    pgvector and the other extensions create their own tables and types. Autogenerate
    sees them as undeclared and proposes dropping them.
    """
    if type_ == "table" and name is not None:
        return name in target_metadata.tables
    return True


def _render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    """Render pgvector column types with the import they need.

    Autogenerate emits `pgvector.sqlalchemy.vector.VECTOR(dim=768)` and no matching
    import, so the migration raises NameError the first time it runs. Returning the
    rendering here, and registering the import, makes generated migrations runnable
    without hand-editing.
    """
    if type_ == "type" and obj.__class__.__module__.startswith("pgvector"):
        autogen_context.imports.add("import pgvector.sqlalchemy")
        return f"pgvector.sqlalchemy.Vector({obj.dim})"
    return False


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=_include_object,
        render_item=_render_item,
        # Without this, a column type change produces a migration that silently does
        # nothing, which is worse than one that fails.
        compare_type=True,
        compare_server_default=True,
        # Postgres can do the whole migration in one transaction, so a failure
        # halfway leaves the schema as it was.
        transaction_per_migration=False,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it, for review before a production apply."""
    context.configure(
        url=_owner_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=_include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Apply migrations against a live database."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _owner_url()

    engine = async_engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    async with engine.connect() as connection:
        await connection.run_sync(_run)

    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
