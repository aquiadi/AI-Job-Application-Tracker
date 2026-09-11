"""The schema.

Two rules run through all of it.

**Every tenant-owned table carries `user_id` and is covered by a row-level security
policy.** The column is not a convenience for writing `WHERE` clauses — it is what the
policy compares against, and it is why a query that forgets to filter returns nothing
instead of everything. :class:`TenantMixin` exists so that adding a table without a
`user_id` is a deliberate act rather than an oversight, and :data:`USER_SCOPED_TABLES`
is what the cross-tenant test iterates.

**Every vector column travels with the identity of the space it belongs to.** A
similarity comparison between vectors from different models, widths or task types is
meaningless but not erroneous: it returns a number, and the number is noise.
:class:`EmbeddingMixin` stores all three so a mismatch is detectable.

`jd_extraction_cache` is the one table with no policy. It is global by design, keyed by
content hash, and holds only public job-posting content.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)

# Aliased: several models declare a column named `text`, which shadows the function
# inside the class body and turns `text(...)` into a call on a MappedColumn.
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from jobtrack_core.db.base import (
    EMBEDDING_DIM,
    Base,
    CreatedAtMixin,
    TimestampMixin,
    uuid_pk,
)
from jobtrack_core.db.enums import (
    ARTIFACT_KIND_TYPE,
    EMPLOYMENT_TYPE_TYPE,
    JOB_STATUS_TYPE,
    LLM_OPERATION_TYPE,
    LLM_OUTCOME_TYPE,
    NUDGE_OUTCOME_TYPE,
    NUDGE_STATE_TYPE,
    PROFILE_ITEM_KIND_TYPE,
    REMOTE_POLICY_TYPE,
    REQUIREMENT_KIND_TYPE,
    SALARY_PERIOD_TYPE,
    SENIORITY_TYPE,
    SOURCE_ATS_TYPE,
    STAGE_TYPE,
    ArtifactKind,
    EmploymentType,
    JobStatus,
    LlmOperation,
    LlmOutcome,
    NudgeOutcome,
    NudgeState,
    ProfileItemKind,
    RemotePolicy,
    RequirementKind,
    SalaryPeriod,
    Seniority,
    SourceAts,
)
from jobtrack_core.domain.stages import Stage

# --------------------------------------------------------------------------
# Mixins
# --------------------------------------------------------------------------


class TenantMixin:
    """Marks a table as tenant-owned, which is what the RLS policy keys on.

    ``ON DELETE CASCADE`` is how ``DELETE /me`` stays honest: removing the user row
    takes every dependent row with it, rather than relying on an application-level
    sweep that can miss a table added later.
    """

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def _search_index(table: str) -> Index:
    """GIN over the generated tsvector. The lexical arm of ADR 12."""
    return Index(f"ix_{table}_search", "search", postgresql_using="gin")


def _vector_index(table: str) -> Index:
    """HNSW with cosine ops.

    The operator class has to match the distance operator the scoring query uses. A
    mismatch does not error — it leaves the index unused and turns every scoring pass
    into a sequential scan over every profile item.

    m and ef_construction are pgvector's defaults; tuning them without a benchmark
    would be guessing, and that benchmark is deferred in ROADMAP.md.
    """
    return Index(
        f"ix_{table}_embedding",
        "embedding",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


class SearchableMixin:
    """A Postgres-maintained `tsvector` over the row's `text` column.

    The lexical arm of hybrid retrieval (ADR 12). Generated and stored, so there is no
    application code that can forget to keep it current, and `ts_rank_cd` can read the
    value rather than only filter on it.

    `Computed` tells SQLAlchemy the database owns the value, so it is never sent in an
    INSERT — which Postgres rejects outright for a generated column.
    """

    # Nullable because Postgres does not infer NOT NULL for a generated column, even
    # from a NOT NULL source. Declaring otherwise makes the model disagree with the
    # database, which `alembic check` correctly reports as drift.
    search: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
        nullable=True,
    )


class EmbeddingMixin:
    """A vector plus the identity of the space that produced it.

    All five columns are null together: a row exists before the worker has embedded it,
    and the interface shows that as `embedding` rather than `ready`.
    """

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_task_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


class User(Base, TimestampMixin):
    """One row per Identity Platform subject.

    `email` here is the sign-in identity, used to recognise the account. The contact
    email that appears on a rendered resume lives on `profiles`, separately, because
    contact details are excluded from everything sent to a model.

    This table is tenant-scoped too, but its policy keys on `id` rather than `user_id`,
    which is why it is not in :data:`USER_SCOPED_TABLES`.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    subject: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # Nudge thresholds are in business days in the user's own timezone, so this is
    # part of the domain rather than a display preference.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="UTC")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Profile(Base, TenantMixin, TimestampMixin):
    """The master profile. One per user in this version.

    The block of contact fields is deliberately grouped and deliberately separate from
    anything that becomes prompt input. Prompt context is built from `profile_items`
    and the non-contact fields here; these columns are re-attached at render time and
    never travel to Vertex.
    """

    __tablename__ = "profiles"
    __table_args__ = (UniqueConstraint("user_id", name="uq_profiles_user_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Contact details. Never sent to a model.
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    links: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")


class ProfileItem(Base, TenantMixin, TimestampMixin, EmbeddingMixin, SearchableMixin):
    """One bullet, project, skill or qualification, embedded on its own.

    Granularity is the point. A whole resume as one vector answers "is this person
    roughly like this posting". A bullet on its own answers "which specific thing I
    have done covers this specific requirement", which is what the score needs and what
    a citation has to be able to point at.
    """

    __tablename__ = "profile_items"
    __table_args__ = (
        _search_index("profile_items"),
        _vector_index("profile_items"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ProfileItemKind] = mapped_column(PROFILE_ITEM_KIND_TYPE, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    organisation: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    ended_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # An imported item is a model's reading of a resume until the user confirms it.
    # Only reviewed items may be cited by generated content.
    reviewed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("false")
    )


# --------------------------------------------------------------------------
# Postings
# --------------------------------------------------------------------------


class Job(Base, TenantMixin, TimestampMixin):
    """A posting, normalised into the canonical schema.

    Fields the posting does not state stay null. That is a measured property rather
    than an aspiration: the extraction eval reports a hallucinated-field rate over
    exactly these columns.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        # The same posting saved twice by one user is one row. The hash is over
        # normalised text, so a re-paste with different whitespace collapses onto it.
        UniqueConstraint("user_id", "content_hash", name="uq_jobs_user_id_content_hash"),
        CheckConstraint(
            "experience_years_min IS NULL OR experience_years_max IS NULL "
            "OR experience_years_min <= experience_years_max",
            name="experience_range_ordered",
        ),
        CheckConstraint(
            "salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max",
            name="salary_range_ordered",
        ),
        # A failure has to say why. A row in `failed` with no reason is a dead end for
        # whoever is looking at it, which is usually the user.
        CheckConstraint(
            "status <> 'failed' OR failure_reason IS NOT NULL",
            name="failure_has_a_reason",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()

    status: Mapped[JobStatus] = mapped_column(
        JOB_STATUS_TYPE, nullable=False, server_default=JobStatus.QUEUED.value
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_ats: Mapped[SourceAts] = mapped_column(SOURCE_ATS_TYPE, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Bumping this re-extracts every posting from its stored raw payload.
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_gcs_uri: Mapped[str | None] = mapped_column(Text, nullable=True)

    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remote_policy: Mapped[RemotePolicy | None] = mapped_column(REMOTE_POLICY_TYPE, nullable=True)
    employment_type: Mapped[EmploymentType | None] = mapped_column(
        EMPLOYMENT_TYPE_TYPE, nullable=True
    )
    seniority: Mapped[Seniority | None] = mapped_column(SENIORITY_TYPE, nullable=True)
    experience_years_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    experience_years_max: Mapped[int | None] = mapped_column(Integer, nullable=True)

    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    salary_period: Mapped[SalaryPeriod | None] = mapped_column(SALARY_PERIOD_TYPE, nullable=True)

    hard_skills: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    soft_skills: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    responsibilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")


class JobRequirement(Base, TenantMixin, TimestampMixin, EmbeddingMixin, SearchableMixin):
    """One requirement from a posting, embedded on its own.

    `kind` is what makes the score weighted rather than a flat percentage: a missing
    must-have costs more than a missing nice-to-have.
    """

    __tablename__ = "job_requirements"
    __table_args__ = (
        _search_index("job_requirements"),
        _vector_index("job_requirements"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[RequirementKind] = mapped_column(REQUIREMENT_KIND_TYPE, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------


class Application(Base, TenantMixin, TimestampMixin):
    """A posting the user is actually pursuing.

    `stage` and `stage_entered_at` cache what `stage_events` already says. They exist
    because the board and the nudge sweep both filter on them, and deriving them per
    row on every read is a window function this does not need. Analytics do not read
    them; analytics read the events.
    """

    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_applications_user_id_job_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[Stage] = mapped_column(
        STAGE_TYPE, nullable=False, server_default=Stage.SAVED.value
    )
    stage_entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class StageEvent(Base, TenantMixin, CreatedAtMixin):
    """Append-only history of stage changes.

    Every funnel and time-in-stage number is computed from this table. Append-only is
    enforced by grant rather than by convention: the application role holds INSERT and
    SELECT here and nothing else, so an UPDATE is a permission error rather than a
    silently rewritten history.
    """

    __tablename__ = "stage_events"
    __table_args__ = (
        Index("ix_stage_events_application_id_occurred_at", "application_id", "occurred_at"),
        CheckConstraint(
            "from_stage IS NULL OR from_stage <> to_stage", name="stage_actually_moved"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    # Null only on the row that created the application.
    from_stage: Mapped[Stage | None] = mapped_column(STAGE_TYPE, nullable=True)
    to_stage: Mapped[Stage] = mapped_column(STAGE_TYPE, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Artifact(Base, TenantMixin, CreatedAtMixin):
    """A generated document, versioned, with its citations.

    `content` holds the structured document: each bullet with the `source_item_ids` it
    came from. Storing the structure rather than only the rendered PDF is what lets the
    interface diff against the master resume, and what lets the grounding validator be
    re-run over an old artifact when the rules change.

    `warnings` records bullets dropped for failing validation, because a silently
    shorter resume is worse than a visible gap.
    """

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint(
            "application_id", "kind", "version", name="uq_artifacts_application_id_kind_version"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ArtifactKind] = mapped_column(ARTIFACT_KIND_TYPE, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    pdf_gcs_uri: Mapped[str | None] = mapped_column(Text, nullable=True)


class Nudge(Base, TenantMixin, TimestampMixin):
    """A drafted follow-up. Never sent by the system.

    The unique constraint is the real deduplication guarantee. Cloud Tasks names tasks
    deterministically from the same triple, which stops most duplicates, but task-name
    deduplication only holds for a limited window, and a sweep retried outside it would
    enqueue again. The constraint is what makes a second draft impossible.
    """

    __tablename__ = "nudges"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "stage",
            "stage_entered_at",
            name="uq_nudges_application_id_stage_stage_entered_at",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[Stage] = mapped_column(STAGE_TYPE, nullable=False)
    stage_entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    state: Mapped[NudgeState] = mapped_column(
        NUDGE_STATE_TYPE, nullable=False, server_default=NudgeState.PENDING.value
    )
    draft_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[NudgeOutcome | None] = mapped_column(NUDGE_OUTCOME_TYPE, nullable=True)


# --------------------------------------------------------------------------
# Operational
# --------------------------------------------------------------------------


class LlmCall(Base, TenantMixin, CreatedAtMixin):
    """One row per Gemini call. The source of every cost number in the README.

    It holds counts, timings and outcomes, never prompt or response content. A
    validation failure records the *errors*, which name fields, not the text that
    failed.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_created_at_operation", "created_at", "operation"),)

    id: Mapped[uuid.UUID] = uuid_pk()

    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[LlmOperation] = mapped_column(LLM_OPERATION_TYPE, nullable=False)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cached_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # Eight decimal places because one extraction costs a fraction of a cent, and
    # rounding to four would report most calls as free.
    estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 8), nullable=False, server_default="0"
    )

    outcome: Mapped[LlmOutcome] = mapped_column(LLM_OUTCOME_TYPE, nullable=False)
    validation_errors: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class Outbox(Base, TenantMixin, CreatedAtMixin):
    """Domain events, written in the same transaction as the change they describe.

    `id` is the event id. It travels in the Pub/Sub message attributes and is what
    consumers deduplicate on, because the relay publishes at least once by
    construction: a row can be published and the process die before `published_at` is
    written.

    The relay has to read across every tenant, which the tenant policy forbids. It
    will connect as its own role with its own policy rather than bypassing RLS; that
    role arrives with the relay itself in M2.
    """

    __tablename__ = "outbox"
    __table_args__ = (
        # The relay's only query: unpublished rows, oldest first. Partial, so the
        # index stays small as published rows accumulate.
        Index(
            "ix_outbox_unpublished",
            "created_at",
            postgresql_where=sql_text("published_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class JdExtractionCache(Base, CreatedAtMixin):
    """Extraction results keyed by content hash, shared across every user.

    This is the one table with no `user_id` and no policy, and that is load-bearing
    rather than an omission: two users who save the same Greenhouse posting should not
    each pay for an extraction.

    It is safe only because of what it holds. A job posting is public text published by
    an employer. Nothing derived from a resume, a profile, or a user's behaviour may be
    written here. The key includes `schema_version`, so bumping the canonical schema
    re-extracts rather than returning a stale shape.
    """

    __tablename__ = "jd_extraction_cache"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    extracted: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)


# --------------------------------------------------------------------------
# Table classification
#
# The migration builds policies from these tuples and the cross-tenant test iterates
# them, so a new table has to be classified or it fails the test rather than quietly
# shipping without a policy. `test_models.py` asserts the three sets cover the metadata
# exactly.
# --------------------------------------------------------------------------

#: Tenant tables whose policy keys on `user_id`.
USER_SCOPED_TABLES: tuple[str, ...] = (
    "profiles",
    "profile_items",
    "jobs",
    "job_requirements",
    "applications",
    "stage_events",
    "artifacts",
    "nudges",
    "llm_calls",
    "outbox",
)

#: Tenant table whose policy keys on `id`, because it is the user.
SELF_SCOPED_TABLES: tuple[str, ...] = ("users",)

#: Deliberately global. Holds no user-derived data.
GLOBAL_TABLES: tuple[str, ...] = ("jd_extraction_cache",)

#: Append-only: the application role gets INSERT and SELECT, never UPDATE or DELETE.
APPEND_ONLY_TABLES: tuple[str, ...] = ("stage_events",)
