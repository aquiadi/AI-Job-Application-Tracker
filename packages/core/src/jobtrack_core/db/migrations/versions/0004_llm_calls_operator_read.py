"""Let the schema owner read llm_calls, for operational reporting

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-11

`make cost-report` returned nothing while the table held twenty rows, and the reason is
that `FORCE ROW LEVEL SECURITY` is doing exactly what it was set for: it applies to the
table owner as well, and the only policy on `llm_calls` is scoped to `jobtrack_app`. An
owner connection therefore matched no rows.

The fix is a second policy rather than dropping FORCE or running the report as a
superuser. Both of those would remove the protection from every other table to solve a
reporting problem on one.

This is safe only because of what `llm_calls` holds: a model id, a prompt id and
version, token counts, latency, a cost estimate and an outcome. No prompt text, no
response text, no posting or resume content, and the validation errors it stores name
fields rather than values. That is the whole reason this table can have an operator
view and `profile_items` cannot.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNER_ROLE = "jobtrack_owner"


def upgrade() -> None:
    # Read-only: USING without WITH CHECK means this policy permits SELECT and matches
    # nothing for INSERT or UPDATE. The report reads; it must not be able to rewrite
    # the record it is reporting on.
    op.execute(
        f"CREATE POLICY llm_calls_operator_read ON llm_calls FOR SELECT TO {OWNER_ROLE} "
        "USING (true)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS llm_calls_operator_read ON llm_calls")
