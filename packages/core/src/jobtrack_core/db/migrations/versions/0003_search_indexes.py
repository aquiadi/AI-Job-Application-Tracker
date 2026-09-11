"""Add lexical search columns and the vector indexes scoring reads

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-11

Hybrid retrieval (ADR 12) needs both arms indexed.

The `search` columns are generated and stored rather than expression indexes, because
`ts_rank_cd` needs the tsvector value itself at query time, not only a way to filter on
it. Generated means Postgres maintains them; there is no application code that can
forget to update one.

`english` is hardcoded in the generated expression because a generated column requires
an immutable expression, and `to_tsvector(regconfig, text)` is only immutable when the
configuration is a literal. Multilingual postings are noted in ROADMAP.md.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: m and ef_construction are pgvector's defaults. Tuning them without a benchmark
#: would be guessing, and the benchmark is in ROADMAP.md as deferred work.
_HNSW = "WITH (m = 16, ef_construction = 64)"


def upgrade() -> None:
    for table in ("profile_items", "job_requirements"):
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN search tsvector "
            f"GENERATED ALWAYS AS (to_tsvector('english', text)) STORED"
        )
        op.execute(f"CREATE INDEX ix_{table}_search ON {table} USING GIN (search)")

    # Cosine, because the embeddings are normalised and cosine is what the task type
    # and the fit score's thresholds are both defined against. A different operator
    # class here would leave the index unused by the scoring query and silently turn
    # every scoring pass into a sequential scan.
    op.execute(
        "CREATE INDEX ix_profile_items_embedding ON profile_items "
        f"USING hnsw (embedding vector_cosine_ops) {_HNSW}"
    )
    op.execute(
        "CREATE INDEX ix_job_requirements_embedding ON job_requirements "
        f"USING hnsw (embedding vector_cosine_ops) {_HNSW}"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_job_requirements_embedding")
    op.execute("DROP INDEX IF EXISTS ix_profile_items_embedding")
    for table in ("profile_items", "job_requirements"):
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_search")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS search")
