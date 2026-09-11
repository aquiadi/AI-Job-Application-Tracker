"""Building and maintaining the profile the fit score reads.

Two ways in. A user types items directly, or uploads a resume and reviews what the
import found. Both end in the same place: `profile_items` rows, each embedded on its
own, each carrying a `reviewed` flag.

That flag is load-bearing. An imported item is a model's reading of a document until a
person confirms it, and nothing unreviewed is counted by the fit score or citable by
generated content. The alternative — trusting the import — would put a number and a
generated bullet on top of text nobody checked.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobtrack_core.db.enums import LlmOperation, ProfileItemKind
from jobtrack_core.db.models import Profile, ProfileItem
from jobtrack_core.events import PROFILE, EventType
from jobtrack_core.llm.client import EmbeddingTaskType, LlmClient
from jobtrack_core.llm.prompts import load_prompt
from jobtrack_core.pipeline.jobs import emit, record_call
from jobtrack_core.profile.pdf import extract_text
from jobtrack_core.profile.schemas import ParsedResume

#: Items embedded per model call.
EMBED_CHUNK = 100


@dataclass(frozen=True, slots=True)
class ImportResult:
    profile_id: uuid.UUID
    imported: int
    headline: str | None


async def ensure_profile(session: AsyncSession, *, user_id: uuid.UUID) -> Profile:
    """Return the user's profile, creating an empty one on first use."""
    await session.execute(
        insert(Profile)
        .values(user_id=user_id)
        .on_conflict_do_nothing(index_elements=[Profile.user_id])
    )
    return (await session.execute(select(Profile).where(Profile.user_id == user_id))).scalar_one()


async def import_resume(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    pdf: bytes,
    client: LlmClient,
    replace: bool = True,
) -> ImportResult:
    """Parse an uploaded resume into unreviewed items.

    `replace` removes previously imported-but-unreviewed items. Re-importing after
    fixing a resume is the normal case, and leaving the previous attempt behind would
    mean reviewing the same content twice.
    """
    profile = await ensure_profile(session, user_id=user_id)
    text = extract_text(pdf)

    prompt = load_prompt("parse_resume").render(resume=text)
    result = await client.generate_structured(prompt, ParsedResume)
    await record_call(
        session,
        user_id=user_id,
        model=result.model,
        prompt_id=result.prompt_id,
        prompt_version=result.prompt_version,
        operation=LlmOperation.PARSE_RESUME,
        usage=result.usage,
        latency_ms=result.latency_ms,
        cost=result.estimated_cost_usd,
        attempts=result.attempts,
        validation_errors=result.validation_errors,
    )

    if replace:
        await session.execute(
            delete(ProfileItem).where(
                ProfileItem.profile_id == profile.id, ProfileItem.reviewed.is_(False)
            )
        )

    highest = (
        await session.execute(
            select(ProfileItem.display_order)
            .where(ProfileItem.profile_id == profile.id)
            .order_by(ProfileItem.display_order.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    rows = [
        ProfileItem(
            user_id=user_id,
            profile_id=profile.id,
            kind=item.kind,
            text=item.text,
            organisation=item.organisation,
            role=item.role,
            started_on=item.started_on,
            ended_on=item.ended_on,
            display_order=(highest or 0) + offset + 1,
            reviewed=False,
        )
        for offset, item in enumerate(result.value.items)
    ]
    session.add_all(rows)

    if result.value.headline and not profile.headline:
        profile.headline = result.value.headline

    await session.flush()
    await embed_items(session, user_id=user_id, client=client)
    await emit(
        session,
        user_id=user_id,
        event_type=EventType.PROFILE_IMPORTED,
        aggregate_type=PROFILE,
        aggregate_id=profile.id,
        payload={"items": len(rows), "model": result.model},
    )

    return ImportResult(profile_id=profile.id, imported=len(rows), headline=profile.headline)


async def embed_items(session: AsyncSession, *, user_id: uuid.UUID, client: LlmClient) -> int:
    """Embed every profile item that does not have a current vector.

    Synchronous rather than queued. A profile edit is a handful of rows and the user is
    looking at the screen: making them wait for a background pass to see their own edit
    scored would be the wrong trade. A resume import is larger but still one batched
    call.
    """
    pending = list(
        (
            await session.execute(
                select(ProfileItem).where(
                    ProfileItem.user_id == user_id, ProfileItem.embedding.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    if not pending:
        return 0

    embedded_at = datetime.now(UTC)
    for offset in range(0, len(pending), EMBED_CHUNK):
        chunk = pending[offset : offset + EMBED_CHUNK]
        result = await client.embed(
            [item.text for item in chunk], task_type=EmbeddingTaskType.SEMANTIC_SIMILARITY
        )
        for item, vector in zip(chunk, result.vectors, strict=True):
            item.embedding = vector
            item.embedding_model = result.model
            item.embedding_dim = result.dim
            item.embedding_task_type = result.task_type.value
            item.embedded_at = embedded_at

        await record_call(
            session,
            user_id=user_id,
            model=result.model,
            prompt_id="embed",
            prompt_version="v1",
            operation=LlmOperation.EMBED,
            usage=result.usage,
            latency_ms=result.latency_ms,
            cost=result.estimated_cost_usd,
        )

    await session.flush()
    return len(pending)


async def clear_embedding(item: ProfileItem) -> None:
    """Drop an item's vector so the next pass re-embeds it.

    Called when the text changes. Leaving the old vector would mean the fit score
    compares against wording the user has already replaced, which is the kind of bug
    that shows up as a score that will not move.
    """
    item.embedding = None
    item.embedding_model = None
    item.embedding_dim = None
    item.embedding_task_type = None
    item.embedded_at = None


def kinds() -> tuple[ProfileItemKind, ...]:
    return tuple(ProfileItemKind)
