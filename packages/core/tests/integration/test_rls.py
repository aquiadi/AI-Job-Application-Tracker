"""Proof that one user cannot read another user's rows.

This is the test the whole tenancy design exists for, and it is a gate rather than a
nicety: if it fails, nothing else in the system matters.

It asserts four separate things, because "RLS is on" is four claims in a trench coat:

1. A second user reads zero rows from every tenant table.
2. A session with no tenant context reads zero rows from every tenant table, so a
   missing `SET` fails closed rather than open.
3. A user cannot write a row stamped with someone else's `user_id`, which is the
   `WITH CHECK` half that a read-only test would miss entirely.
4. Every table the application knows about actually has a policy, so adding a table
   without one fails here instead of shipping.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jobtrack_core.db.models import (
    GLOBAL_TABLES,
    SELF_SCOPED_TABLES,
    USER_SCOPED_TABLES,
)
from jobtrack_core.db.session import privileged_session, tenant_session
from jobtrack_core.domain.stages import Stage

pytestmark = pytest.mark.integration


#: Every tenant table, and how to count rows in it.
ALL_TENANT_TABLES = tuple(SELF_SCOPED_TABLES) + tuple(USER_SCOPED_TABLES)


async def _seed_user(session: AsyncSession, user_id: uuid.UUID, label: str) -> dict[str, uuid.UUID]:
    """Write one row into every tenant table for ``user_id``.

    Raw SQL rather than the ORM: the point is to exercise the policies, and building an
    object graph through relationships would obscure which statement is being checked.
    Ids are generated here so assertions can name them.
    """
    ids = {name: uuid.uuid4() for name in ALL_TENANT_TABLES}
    now = datetime.now(UTC)

    await session.execute(
        text(
            "INSERT INTO users (id, subject, email, timezone) VALUES (:id, :subject, :email, 'UTC')"
        ),
        {"id": user_id, "subject": f"idp|{label}", "email": f"{label}@example.test"},
    )
    await session.execute(
        text("INSERT INTO profiles (id, user_id, headline) VALUES (:id, :u, :h)"),
        {"id": ids["profiles"], "u": user_id, "h": f"{label} headline"},
    )
    await session.execute(
        text(
            "INSERT INTO profile_items (id, user_id, profile_id, kind, text) "
            "VALUES (:id, :u, :p, 'skill', :t)"
        ),
        {"id": ids["profile_items"], "u": user_id, "p": ids["profiles"], "t": f"{label} skill"},
    )
    await session.execute(
        text(
            "INSERT INTO jobs (id, user_id, source_ats, content_hash, schema_version, title) "
            "VALUES (:id, :u, 'pasted', :h, 1, :t)"
        ),
        {"id": ids["jobs"], "u": user_id, "h": label * 8, "t": f"{label} job"},
    )
    await session.execute(
        text(
            "INSERT INTO job_requirements (id, user_id, job_id, text, kind) "
            "VALUES (:id, :u, :j, :t, 'must')"
        ),
        {"id": ids["job_requirements"], "u": user_id, "j": ids["jobs"], "t": f"{label} req"},
    )
    await session.execute(
        text(
            "INSERT INTO applications (id, user_id, job_id, stage, stage_entered_at) "
            "VALUES (:id, :u, :j, :s, :now)"
        ),
        {
            "id": ids["applications"],
            "u": user_id,
            "j": ids["jobs"],
            "s": Stage.SAVED.value,
            "now": now,
        },
    )
    await session.execute(
        text(
            "INSERT INTO stage_events (id, user_id, application_id, to_stage, occurred_at) "
            "VALUES (:id, :u, :a, :s, :now)"
        ),
        {
            "id": ids["stage_events"],
            "u": user_id,
            "a": ids["applications"],
            "s": Stage.SAVED.value,
            "now": now,
        },
    )
    await session.execute(
        text(
            "INSERT INTO artifacts (id, user_id, application_id, kind, version, content) "
            "VALUES (:id, :u, :a, 'resume', 1, '{}'::jsonb)"
        ),
        {"id": ids["artifacts"], "u": user_id, "a": ids["applications"]},
    )
    await session.execute(
        text(
            "INSERT INTO nudges (id, user_id, application_id, stage, stage_entered_at) "
            "VALUES (:id, :u, :a, :s, :now)"
        ),
        {
            "id": ids["nudges"],
            "u": user_id,
            "a": ids["applications"],
            "s": Stage.APPLIED.value,
            "now": now,
        },
    )
    await session.execute(
        text(
            "INSERT INTO llm_calls "
            "(id, user_id, model, prompt_id, prompt_version, operation, outcome) "
            "VALUES (:id, :u, 'test-model', 'p', 'v1', 'extract_jd', 'ok')"
        ),
        {"id": ids["llm_calls"], "u": user_id},
    )
    await session.execute(
        text(
            "INSERT INTO outbox (id, user_id, aggregate_type, aggregate_id, event_type, payload) "
            "VALUES (:id, :u, 'job', :agg, 'job.extracted', '{}'::jsonb)"
        ),
        {"id": ids["outbox"], "u": user_id, "agg": ids["jobs"]},
    )
    return ids


async def _count(session: AsyncSession, table: str) -> int:
    # Table names are interpolated because an identifier cannot be a bind parameter.
    # They come from ALL_TENANT_TABLES, a module constant derived from the models, so
    # nothing here is reachable from input.
    result = await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
    return int(result.scalar_one())


@pytest.fixture
def alice() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def bob() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture(autouse=True)
async def _clean(owner_engine: AsyncEngine) -> Any:
    """Empty every tenant table before and after each test.

    Truncating as the owner, with CASCADE, because the point of the test is what the
    application role cannot do.
    """
    tables = ", ".join(ALL_TENANT_TABLES)

    async def wipe() -> None:
        async with owner_engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} CASCADE"))

    await wipe()
    yield
    await wipe()


class TestCrossTenantReads:
    async def test_a_second_user_reads_zero_rows_from_every_tenant_table(
        self,
        sessions: async_sessionmaker[AsyncSession],
        alice: uuid.UUID,
        bob: uuid.UUID,
    ) -> None:
        async with tenant_session(sessions, alice) as session:
            await _seed_user(session, alice, "alice")
        async with tenant_session(sessions, bob) as session:
            await _seed_user(session, bob, "bob")

        # Alice sees exactly her own row in each table, and Bob's is invisible.
        async with tenant_session(sessions, alice) as session:
            for table in ALL_TENANT_TABLES:
                assert await _count(session, table) == 1, f"alice should see 1 row in {table}"

        async with tenant_session(sessions, bob) as session:
            for table in ALL_TENANT_TABLES:
                assert await _count(session, table) == 1, f"bob should see 1 row in {table}"

    async def test_alice_cannot_read_bobs_rows_by_primary_key(
        self,
        sessions: async_sessionmaker[AsyncSession],
        alice: uuid.UUID,
        bob: uuid.UUID,
    ) -> None:
        # Counting proves isolation in aggregate. Asking for a specific id Alice
        # happens to know proves it for the case that actually worries me.
        async with tenant_session(sessions, alice) as session:
            await _seed_user(session, alice, "alice")
        async with tenant_session(sessions, bob) as session:
            bob_ids = await _seed_user(session, bob, "bob")

        async with tenant_session(sessions, alice) as session:
            for table in ALL_TENANT_TABLES:
                target = bob if table == "users" else bob_ids[table]
                result = await session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE id = :id"),  # noqa: S608
                    {"id": target},
                )
                assert result.scalar_one() == 0, f"alice reached bob's row in {table}"


class TestDefaultDeny:
    async def test_a_session_with_no_tenant_reads_nothing(
        self,
        sessions: async_sessionmaker[AsyncSession],
        alice: uuid.UUID,
    ) -> None:
        # A missing SET has to fail closed. If the policy compared against something
        # that defaulted to permissive, forgetting to set the tenant would expose
        # every row in the database rather than none.
        async with tenant_session(sessions, alice) as session:
            await _seed_user(session, alice, "alice")

        async with privileged_session(sessions) as session:
            for table in ALL_TENANT_TABLES:
                assert await _count(session, table) == 0, f"{table} leaked without a tenant"

    async def test_an_empty_tenant_setting_does_not_raise(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        # `''::uuid` raises. Without the NULLIF guard in the policy, an empty setting
        # turns every query into a 500 instead of an empty result.
        async with privileged_session(sessions) as session:
            await session.execute(text("SELECT set_config('app.user_id', '', true)"))
            assert await _count(session, "jobs") == 0


class TestCrossTenantWrites:
    async def test_a_user_cannot_stamp_a_row_with_someone_elses_user_id(
        self,
        sessions: async_sessionmaker[AsyncSession],
        alice: uuid.UUID,
        bob: uuid.UUID,
    ) -> None:
        # The WITH CHECK half. A read-only test passes happily while writes are
        # completely unprotected.
        async with tenant_session(sessions, alice) as session:
            await _seed_user(session, alice, "alice")
        async with tenant_session(sessions, bob) as session:
            await _seed_user(session, bob, "bob")

        with pytest.raises(DBAPIError) as caught:
            async with tenant_session(sessions, alice) as session:
                await session.execute(
                    text(
                        "INSERT INTO jobs (user_id, source_ats, content_hash, schema_version) "
                        "VALUES (:u, 'pasted', 'forged', 1)"
                    ),
                    {"u": bob},
                )

        assert "row-level security" in str(caught.value).lower()

    async def test_a_user_cannot_reassign_their_row_to_another_user(
        self,
        sessions: async_sessionmaker[AsyncSession],
        alice: uuid.UUID,
        bob: uuid.UUID,
    ) -> None:
        async with tenant_session(sessions, alice) as session:
            await _seed_user(session, alice, "alice")
        async with tenant_session(sessions, bob) as session:
            await _seed_user(session, bob, "bob")

        with pytest.raises(DBAPIError):
            async with tenant_session(sessions, alice) as session:
                await session.execute(text("UPDATE jobs SET user_id = :u"), {"u": bob})

    async def test_a_user_cannot_delete_another_users_rows(
        self,
        sessions: async_sessionmaker[AsyncSession],
        alice: uuid.UUID,
        bob: uuid.UUID,
    ) -> None:
        async with tenant_session(sessions, alice) as session:
            await _seed_user(session, alice, "alice")
        async with tenant_session(sessions, bob) as session:
            await _seed_user(session, bob, "bob")

        # A DELETE with no WHERE clause is the shape of an accident. It has to be
        # confined to the caller's own rows rather than refused, because the policy
        # filters rather than errors.
        async with tenant_session(sessions, alice) as session:
            await session.execute(text("DELETE FROM jobs"))

        async with tenant_session(sessions, bob) as session:
            assert await _count(session, "jobs") == 1


class TestAppendOnlyHistory:
    async def test_stage_events_cannot_be_updated(
        self, sessions: async_sessionmaker[AsyncSession], alice: uuid.UUID
    ) -> None:
        # Every funnel number is derived from this table. Rewriting it would change
        # history silently, so the application role simply has no UPDATE grant.
        async with tenant_session(sessions, alice) as session:
            await _seed_user(session, alice, "alice")

        with pytest.raises(ProgrammingError, match="permission denied"):
            async with tenant_session(sessions, alice) as session:
                await session.execute(
                    text("UPDATE stage_events SET to_stage = :s"), {"s": Stage.OFFER.value}
                )

    async def test_stage_events_cannot_be_deleted(
        self, sessions: async_sessionmaker[AsyncSession], alice: uuid.UUID
    ) -> None:
        async with tenant_session(sessions, alice) as session:
            await _seed_user(session, alice, "alice")

        with pytest.raises(ProgrammingError, match="permission denied"):
            async with tenant_session(sessions, alice) as session:
                await session.execute(text("DELETE FROM stage_events"))


class TestPolicyCoverage:
    async def test_every_tenant_table_has_a_policy_with_force_enabled(
        self, owner_engine: AsyncEngine
    ) -> None:
        # Catches the drift the frozen list in the migration cannot: a table added to
        # the models without a migration that installs its policy.
        async with owner_engine.begin() as conn:
            rows = await conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                    "       count(p.policyname) AS policies "
                    "FROM pg_class c "
                    "LEFT JOIN pg_policies p ON p.tablename = c.relname "
                    "WHERE c.relkind = 'r' AND c.relnamespace = 'public'::regnamespace "
                    "GROUP BY c.relname, c.relrowsecurity, c.relforcerowsecurity"
                )
            )
            state = {r[0]: (r[1], r[2], r[3]) for r in rows}

        for table in ALL_TENANT_TABLES:
            enabled, forced, policies = state[table]
            assert enabled, f"{table} does not have row-level security enabled"
            assert forced, f"{table} does not FORCE row-level security, so its owner bypasses it"
            assert policies >= 1, (
                f"{table} has row-level security on but no policy, so it denies all"
            )

    async def test_the_global_cache_is_deliberately_unprotected(
        self, owner_engine: AsyncEngine
    ) -> None:
        # Asserted rather than assumed, so that turning RLS on here later is a
        # conscious change rather than a silent one.
        async with owner_engine.begin() as conn:
            result = await conn.execute(
                text(
                    "SELECT relrowsecurity FROM pg_class "
                    "WHERE relname = :name AND relnamespace = 'public'::regnamespace"
                ),
                {"name": GLOBAL_TABLES[0]},
            )
            assert result.scalar_one() is False

    async def test_the_application_role_is_not_a_superuser_and_owns_nothing(
        self, owner_engine: AsyncEngine
    ) -> None:
        # If this were ever untrue, every assertion above would pass vacuously.
        async with owner_engine.begin() as conn:
            superuser = await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'jobtrack_app'")
            )
            is_super, bypasses = superuser.one()
            assert not is_super
            assert not bypasses

            owned = await conn.execute(
                text(
                    "SELECT count(*) FROM pg_class "
                    "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' "
                    "AND pg_get_userbyid(relowner) = 'jobtrack_app'"
                )
            )
            assert owned.scalar_one() == 0
