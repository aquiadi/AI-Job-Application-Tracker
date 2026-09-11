"""Enumerations, and the Postgres types that back them.

Native Postgres enums rather than text plus a check constraint: the type is
self-documenting in the database, psql shows a readable value, and a typo becomes an
error at write time. Adding a member later is ``ALTER TYPE ... ADD VALUE``, supported
since Postgres 12.

Each Postgres type is built exactly once and the instance is reused by every column
that needs it. Building a second ``Enum`` with the same name produces a second
``CREATE TYPE`` in the migration, which fails on the way in.
"""

from __future__ import annotations

import enum

from sqlalchemy import Enum as SAEnum

from jobtrack_core.domain.stages import Stage


class ProfileItemKind(enum.StrEnum):
    EXPERIENCE_BULLET = "experience_bullet"
    PROJECT = "project"
    SKILL = "skill"
    EDUCATION = "education"


class RequirementKind(enum.StrEnum):
    MUST = "must"
    NICE = "nice"


class JobStatus(enum.StrEnum):
    """What ingestion has managed so far. Surfaced directly in the interface."""

    QUEUED = "queued"
    EXTRACTING = "extracting"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"


class SourceAts(enum.StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    LINKEDIN = "linkedin"
    GENERIC = "generic"
    PASTED = "pasted"


class RemotePolicy(enum.StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"


class EmploymentType(enum.StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"


class Seniority(enum.StrEnum):
    INTERN = "intern"
    ENTRY = "entry"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"
    LEAD = "lead"
    MANAGER = "manager"
    DIRECTOR = "director"
    EXECUTIVE = "executive"


class SalaryPeriod(enum.StrEnum):
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"
    YEAR = "year"


class ArtifactKind(enum.StrEnum):
    RESUME = "resume"
    COVER_LETTER = "cover_letter"


class NudgeState(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EDITED = "edited"
    DISMISSED = "dismissed"
    SNOOZED = "snoozed"


class NudgeOutcome(enum.StrEnum):
    """Recorded after the fact, so nudge usefulness becomes measurable."""

    SENT = "sent"
    REPLIED = "replied"
    NO_REPLY = "no_reply"


class LlmOperation(enum.StrEnum):
    EXTRACT_JD = "extract_jd"
    EMBED = "embed"
    PARSE_RESUME = "parse_resume"
    TAILOR = "tailor"
    NARRATIVE = "narrative"
    NUDGE_DRAFT = "nudge_draft"
    FAITHFULNESS_JUDGE = "faithfulness_judge"


class LlmOutcome(enum.StrEnum):
    OK = "ok"
    INVALID_OUTPUT = "invalid_output"
    ERROR = "error"
    DEAD_LETTERED = "dead_lettered"


def _pg(enum_cls: type[enum.StrEnum], name: str) -> SAEnum:
    """A native Postgres enum storing the member *value*, not its Python name."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        create_constraint=False,
        values_callable=lambda cls: [member.value for member in cls],
    )


# One instance per Postgres type, shared by every column that uses it.
STAGE_TYPE = _pg(Stage, "application_stage")
PROFILE_ITEM_KIND_TYPE = _pg(ProfileItemKind, "profile_item_kind")
REQUIREMENT_KIND_TYPE = _pg(RequirementKind, "requirement_kind")
JOB_STATUS_TYPE = _pg(JobStatus, "job_status")
SOURCE_ATS_TYPE = _pg(SourceAts, "source_ats")
REMOTE_POLICY_TYPE = _pg(RemotePolicy, "remote_policy")
EMPLOYMENT_TYPE_TYPE = _pg(EmploymentType, "employment_type")
SENIORITY_TYPE = _pg(Seniority, "seniority")
SALARY_PERIOD_TYPE = _pg(SalaryPeriod, "salary_period")
ARTIFACT_KIND_TYPE = _pg(ArtifactKind, "artifact_kind")
NUDGE_STATE_TYPE = _pg(NudgeState, "nudge_state")
NUDGE_OUTCOME_TYPE = _pg(NudgeOutcome, "nudge_outcome")
LLM_OPERATION_TYPE = _pg(LlmOperation, "llm_operation")
LLM_OUTCOME_TYPE = _pg(LlmOutcome, "llm_outcome")
