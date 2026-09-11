"""Finding applications that have gone quiet, and drafting a follow-up.

Two rules shape everything here.

**Nothing is ever sent.** A nudge is a draft in the interface with an edit box and a
copy button. The system has no mail credentials and no send path, and the reason is not
caution about spam — it is that a follow-up sent by software on someone's behalf, in
their name, to a recruiter, is a thing they cannot take back and did not read.

**A nudge is never created twice for the same wait.** The unique constraint on
`(application_id, stage, stage_entered_at)` is the real guarantee. Task-name
deduplication in Cloud Tasks helps, and only within a limited window; a sweep retried
outside that window would enqueue again, and the constraint is what makes the second
draft impossible rather than merely unlikely.

Thresholds are in business days because a Friday application is not stale on Sunday.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobtrack_core.db.enums import LlmOperation, NudgeState
from jobtrack_core.db.models import Application, Job, Nudge
from jobtrack_core.domain.stages import Stage, is_active
from jobtrack_core.llm.client import LlmClient, LlmError
from jobtrack_core.llm.heuristic import HEURISTIC_MODEL
from jobtrack_core.llm.prompts import load_prompt
from jobtrack_core.logs import get_logger
from jobtrack_core.pipeline.jobs import record_call
from jobtrack_core.profile.schemas import NudgeDraft

log = get_logger(__name__)

#: Business days a stage may sit before a follow-up is worth drafting.
#:
#: These are judgement, not measurement, and they are configuration rather than
#: constants for that reason. `Saved` is absent on purpose: an application the user has
#: not sent is not waiting on anyone, and nudging them about their own inaction is the
#: fastest way to make people stop opening the app.
THRESHOLDS: dict[Stage, int] = {
    Stage.APPLIED: 10,
    Stage.SCREEN: 5,
    Stage.TECHNICAL: 5,
    Stage.ONSITE: 5,
    Stage.OFFER: 3,
}


@dataclass(frozen=True, slots=True)
class SweepResult:
    considered: int
    drafted: int
    already_had_one: int


def business_days_between(start: date, end: date) -> int:
    """Whole weekdays from `start` to `end`.

    Weekends only. Public holidays differ by country and the user's country is not
    something this system knows, so pretending to handle them would be worse than
    visibly not handling them.
    """
    if end <= start:
        return 0
    days = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def is_stale(stage: Stage, stage_entered_at: datetime, *, now: datetime) -> bool:
    """True when this stage has waited past its threshold."""
    threshold = THRESHOLDS.get(stage)
    if threshold is None or not is_active(stage):
        return False
    return business_days_between(stage_entered_at.date(), now.date()) >= threshold


async def sweep(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    client: LlmClient,
    now: datetime | None = None,
) -> SweepResult:
    """Draft a follow-up for every application that has waited too long."""
    moment = now or datetime.now(UTC)
    rows = (
        await session.execute(select(Application, Job).join(Job, Job.id == Application.job_id))
    ).all()

    considered = drafted = existing = 0
    for application, job in rows:
        if not is_stale(application.stage, application.stage_entered_at, now=moment):
            continue
        considered += 1

        # ON CONFLICT DO NOTHING against the unique triple, not a read-then-write: two
        # sweeps overlapping is the normal case when a Scheduler backstop fires while a
        # manual sweep is running.
        created = (
            await session.execute(
                insert(Nudge)
                .values(
                    user_id=user_id,
                    application_id=application.id,
                    stage=application.stage,
                    stage_entered_at=application.stage_entered_at,
                    state=NudgeState.PENDING,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        Nudge.application_id,
                        Nudge.stage,
                        Nudge.stage_entered_at,
                    ]
                )
                .returning(Nudge.id)
            )
        ).scalar_one_or_none()

        if created is None:
            existing += 1
            continue

        draft = await draft_for(
            session,
            user_id=user_id,
            job=job,
            stage=application.stage,
            days=business_days_between(application.stage_entered_at.date(), moment.date()),
            client=client,
        )
        nudge = (await session.execute(select(Nudge).where(Nudge.id == created))).scalar_one()
        nudge.draft_subject = draft.subject
        nudge.draft_body = draft.body
        drafted += 1

    await session.flush()
    return SweepResult(considered=considered, drafted=drafted, already_had_one=existing)


async def draft_for(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    job: Job,
    stage: Stage,
    days: int,
    client: LlmClient,
) -> NudgeDraft:
    """Write the follow-up. Falls back to a template if the model is unavailable."""
    role = job.title or "the role"
    company = job.company or "the company"

    if client.generate_model == HEURISTIC_MODEL:
        return _template(role=role, company=company, stage=stage, days=days)

    prompt = load_prompt("nudge_draft").render(
        role=role, company=company, stage=stage.value, days=str(days)
    )
    try:
        answer = await client.generate_structured(prompt, NudgeDraft)
    except LlmError:
        # A follow-up the user can edit beats no follow-up. The template is visibly a
        # template, which is the honest failure mode.
        log.warning("nudge_draft_fell_back_to_template", stage=stage.value)
        return _template(role=role, company=company, stage=stage, days=days)

    await record_call(
        session,
        user_id=user_id,
        model=answer.model,
        prompt_id=answer.prompt_id,
        prompt_version=answer.prompt_version,
        operation=LlmOperation.NUDGE_DRAFT,
        usage=answer.usage,
        latency_ms=answer.latency_ms,
        cost=answer.estimated_cost_usd,
        attempts=answer.attempts,
        validation_errors=answer.validation_errors,
    )
    return answer.value


def _template(*, role: str, company: str, stage: Stage, days: int) -> NudgeDraft:
    """A plain, editable starting point.

    Written to be sent as-is by someone in a hurry and obviously worth editing by
    someone who is not. No name and no signature: the user adds those, because this
    system does not have them and will not guess.
    """
    after_interview = stage in {Stage.SCREEN, Stage.TECHNICAL, Stage.ONSITE}
    if stage is Stage.OFFER:
        body = (
            f"Thank you again for the offer for the {role} role. "
            "I am working through it carefully and want to make sure I have everything I need. "
            "Could you let me know the timeline you are working to, and who I should speak to "
            "with questions about the details?"
        )
    elif after_interview:
        body = (
            f"Thank you for the time your team spent with me on the {role} role. "
            "I enjoyed the conversation and remain very interested. "
            f"It has been about {days} working days, so I wanted to check whether there is "
            "anything further you need from me, and what the timing looks like from here."
        )
    else:
        body = (
            f"I applied for the {role} role at {company} about {days} working days ago and "
            "wanted to restate my interest. "
            "I am happy to share anything that would help, including references or a short "
            "walkthrough of relevant work. "
            "Is there an update on where the process stands?"
        )

    return NudgeDraft(subject=f"Following up on {role}", body=body)
