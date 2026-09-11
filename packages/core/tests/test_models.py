"""Guards on the schema's shape that do not need a database.

The cross-tenant integration test proves the policies work. These prove the *inputs*
to those policies are complete, which is the failure mode that would otherwise ship
quietly: a new table added without being classified gets no policy, and a test that
iterates a stale list will not notice.
"""

from __future__ import annotations

import pytest

from jobtrack_core.db.base import EMBEDDING_DIM, Base
from jobtrack_core.db.models import (
    APPEND_ONLY_TABLES,
    GLOBAL_TABLES,
    SELF_SCOPED_TABLES,
    USER_SCOPED_TABLES,
)

ALL_CLASSIFIED = set(USER_SCOPED_TABLES) | set(SELF_SCOPED_TABLES) | set(GLOBAL_TABLES)


class TestClassificationIsComplete:
    def test_every_table_is_classified_exactly_once(self) -> None:
        # If this fails, a table was added without deciding whether it is tenant-owned.
        # That decision cannot be defaulted: defaulting to global leaks, and defaulting
        # to tenant-scoped breaks a table that genuinely has no owner.
        defined = set(Base.metadata.tables)

        assert defined == ALL_CLASSIFIED, (
            f"unclassified: {sorted(defined - ALL_CLASSIFIED)}, "
            f"classified but missing: {sorted(ALL_CLASSIFIED - defined)}"
        )

        total = len(USER_SCOPED_TABLES) + len(SELF_SCOPED_TABLES) + len(GLOBAL_TABLES)
        assert total == len(ALL_CLASSIFIED), "a table is in more than one classification"

    def test_subsidiary_lists_refer_to_real_tables(self) -> None:
        for name in APPEND_ONLY_TABLES:
            assert name in Base.metadata.tables


class TestTenantColumns:
    @pytest.mark.parametrize("table_name", USER_SCOPED_TABLES)
    def test_user_scoped_tables_have_a_non_null_user_id(self, table_name: str) -> None:
        column = Base.metadata.tables[table_name].columns.get("user_id")

        assert column is not None, f"{table_name} is user-scoped but has no user_id"
        assert not column.nullable, (
            f"{table_name}.user_id is nullable, so a row can exist that no policy "
            "matches and nobody can read"
        )

    @pytest.mark.parametrize("table_name", USER_SCOPED_TABLES)
    def test_user_scoped_tables_index_user_id(self, table_name: str) -> None:
        # Every policy adds `user_id = ...` to every query against these tables.
        table = Base.metadata.tables[table_name]
        indexed = {
            next(c.name for c in index.columns) for index in table.indexes if len(index.columns)
        }
        assert "user_id" in indexed or table.columns["user_id"].index

    def test_the_global_table_has_no_user_id(self) -> None:
        # A user_id here would mean user-derived data reached a table shared across
        # every tenant, which is the one thing this table must never hold.
        for name in GLOBAL_TABLES:
            assert "user_id" not in Base.metadata.tables[name].columns


class TestEmbeddingColumns:
    def test_every_vector_column_carries_its_space(self) -> None:
        # A vector without its model, width and task type can be compared against a
        # vector from a different space. That returns a number rather than an error,
        # and the number is noise.
        for table in Base.metadata.tables.values():
            if "embedding" not in table.columns:
                continue
            for companion in ("embedding_model", "embedding_dim", "embedding_task_type"):
                assert companion in table.columns, f"{table.name} has embedding but no {companion}"

    def test_vector_width_matches_the_configured_default(self) -> None:
        # pgvector needs a fixed width to build an index, so the schema pins it and
        # settings validate against it. 768 keeps it under pgvector's 2000-dimension
        # index limit for the `vector` type.
        assert EMBEDDING_DIM == 768
        assert EMBEDDING_DIM <= 2000


class TestAppendOnlyIntent:
    def test_stage_events_has_no_updated_at(self) -> None:
        # An `updated_at` on an append-only table invites an UPDATE. The grant is the
        # real control; the absent column is the signal.
        assert "updated_at" not in Base.metadata.tables["stage_events"].columns
