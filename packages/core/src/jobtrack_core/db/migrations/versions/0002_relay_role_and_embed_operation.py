"""Give the outbox relay its own role, and name embedding as an operation

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-11

The relay's job is the one thing the tenant policy forbids: reading rows that belong to
every user. There are two ways to allow that. One is to let the relay connect as the
application role and bypass row-level security, which widens the application's own
access to the same degree and puts a bypass in the codebase for a bug to find. The
other is a second role whose policy covers `outbox` and nothing else.

This is the second. `jobtrack_relay` can read and mark published rows in `outbox`. It
holds no grant at all on any other table, so a compromised or simply buggy relay cannot
read a resume: the failure is a permission error, not a leak.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RELAY_ROLE = "jobtrack_relay"
APP_ROLE = "jobtrack_app"


def upgrade() -> None:
    # Postgres 12 and later permit ADD VALUE inside a transaction as long as the new
    # value is not used in the same transaction. Nothing here uses it.
    op.execute("ALTER TYPE llm_operation ADD VALUE IF NOT EXISTS 'embed'")

    # The migration does not create the role, and cannot: jobtrack_owner is
    # NOCREATEROLE on purpose, so a schema migration has no authority over principals.
    # That split is worth keeping — a migration manages tables, and whoever manages
    # identities is a different, more privileged actor. Locally the compose bootstrap
    # creates the role; in AlloyDB it is an IAM principal.
    #
    # So this asserts rather than creates, and says exactly what to run if it is
    # missing, because a relay silently left without grants would look like an outbox
    # that never drains.
    op.execute(
        f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{RELAY_ROLE}') THEN
            RAISE EXCEPTION
              'role {RELAY_ROLE} does not exist. Create it before migrating: '
              'CREATE ROLE {RELAY_ROLE} LOGIN PASSWORD ... NOCREATEDB NOCREATEROLE NOSUPERUSER; '
              'locally, make clean && make up re-runs the compose bootstrap.';
          END IF;
        END $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RELAY_ROLE}")

    # SELECT to find unpublished rows, UPDATE to mark them published. No INSERT: the
    # relay publishes events, it does not author them. No DELETE: pruning published
    # rows is a separate job with its own role, and a relay that could delete could
    # destroy an audit trail on a retry.
    op.execute(f"GRANT SELECT, UPDATE ON outbox TO {RELAY_ROLE}")

    # `USING (true)` is the whole point of this role, and it is safe only because the
    # grant above is the only grant it has. The policy is scoped to this role, so the
    # application's own policy on `outbox` is untouched.
    op.execute(
        f"CREATE POLICY outbox_relay ON outbox FOR ALL TO {RELAY_ROLE} "
        "USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS outbox_relay ON outbox")
    op.execute(f"REVOKE ALL ON outbox FROM {RELAY_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {RELAY_ROLE}")
    # The role itself is left in place: it may be an IAM principal that other
    # databases in the cluster still reference, and dropping it here would fail.
    #
    # The enum value is also left. Postgres has no ALTER TYPE ... DROP VALUE, and
    # rebuilding the type would rewrite every llm_calls row to undo one addition.
