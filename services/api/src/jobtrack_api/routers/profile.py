"""The user's profile: the evidence every score and every generated bullet draws on.

The contact block is separated from everything else here, not as a presentation choice
but as the enforcement point for a rule stated in CLAUDE.md: contact details never
reach a model. They live on their own columns, they are absent from `ParsedResume` and
from every prompt, and they are re-attached when a document is rendered. `GET /profile`
returns them because the browser is not a model.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, File, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from jobtrack_api.deps.auth import CurrentIdentity, TenantSession
from jobtrack_api.deps.runtime import AppRuntime
from jobtrack_api.errors import (
    NotFoundError,
    UnauthenticatedError,
    UnprocessableInputError,
    error_responses,
)
from jobtrack_core.db.enums import ProfileItemKind
from jobtrack_core.db.models import Profile, ProfileItem
from jobtrack_core.llm.client import LlmError
from jobtrack_core.pipeline import profile as pipeline
from jobtrack_core.profile.pdf import ResumeReadError
from jobtrack_core.profile.schemas import MIN_PROSE_CHARS, MIN_SKILL_CHARS

router = APIRouter(prefix="/profile", tags=["profile"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class Contact(BaseModel):
    """Re-attached when a document is rendered. Never sent to a model."""

    full_name: str | None = Field(default=None, max_length=255)
    contact_email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=255)
    links: list[str] = Field(default_factory=list, max_length=10)


class ProfileUpdate(Contact):
    headline: str | None = Field(default=None, max_length=255)
    summary: str | None = Field(default=None, max_length=2000)


class ItemIn(BaseModel):
    """A new profile item.

    The minimum length is one character, not three, because a skill can be "R", "Go"
    or "C#" — precisely the tokens a posting screens on. A blanket three-character
    floor silently refused the most specific things a person can claim. Prose is held
    to a longer minimum by the validator below, where the two cases can differ.
    """

    text: str = Field(min_length=MIN_SKILL_CHARS, max_length=600)
    kind: ProfileItemKind = ProfileItemKind.EXPERIENCE_BULLET
    organisation: str | None = Field(default=None, max_length=255)
    role: str | None = Field(default=None, max_length=255)
    started_on: date | None = None
    ended_on: date | None = None

    @model_validator(mode="after")
    def _prose_is_long_enough(self) -> ItemIn:
        if self.kind is not ProfileItemKind.SKILL and len(self.text.strip()) < MIN_PROSE_CHARS:
            raise ValueError(f"a {self.kind.value} needs at least {MIN_PROSE_CHARS} characters")
        return self


class ItemPatch(BaseModel):
    text: str | None = Field(default=None, min_length=MIN_SKILL_CHARS, max_length=600)
    kind: ProfileItemKind | None = None
    organisation: str | None = Field(default=None, max_length=255)
    role: str | None = Field(default=None, max_length=255)
    started_on: date | None = None
    ended_on: date | None = None
    reviewed: bool | None = None


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    text: str
    kind: ProfileItemKind
    organisation: str | None
    role: str | None
    started_on: date | None
    ended_on: date | None
    display_order: int
    #: False for anything an import produced. Unreviewed items are excluded from the
    #: fit score and may not be cited by generated content.
    reviewed: bool
    #: False until embedded. An item is not comparable until it has a vector.
    embedded: bool


class ProfileOut(ProfileUpdate):
    id: uuid.UUID
    updated_at: datetime
    items: list[ItemOut]
    reviewed_count: int
    unreviewed_count: int


class ImportOut(BaseModel):
    imported: int
    headline: str | None
    #: Everything an import produces needs review before it counts.
    needs_review: int


@router.get("", summary="The user's profile", responses=error_responses(UnauthenticatedError))
async def get_profile(session: TenantSession, identity: CurrentIdentity) -> ProfileOut:
    """The profile and every item on it, in display order."""
    profile = await pipeline.ensure_profile(session, user_id=identity.user_id)
    items = await _items(session, profile.id)
    return _shape(profile, items)


@router.patch(
    "",
    summary="Update contact details and summary",
    responses=error_responses(UnauthenticatedError),
)
async def update_profile(
    body: ProfileUpdate, session: TenantSession, identity: CurrentIdentity
) -> ProfileOut:
    """Update the fields a rendered document carries."""
    profile = await pipeline.ensure_profile(session, user_id=identity.user_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    await session.flush()
    # `updated_at` carries onupdate=now(), so flushing expires it. Reading it without
    # this refresh triggers a lazy load, which on an async session is IO from a place
    # that cannot do IO — it surfaces as MissingGreenlet rather than as a slow query.
    await session.refresh(profile)
    return _shape(profile, await _items(session, profile.id))


@router.post(
    "/items",
    summary="Add an item",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(UnauthenticatedError),
)
async def add_item(
    body: ItemIn, session: TenantSession, identity: CurrentIdentity, runtime: AppRuntime
) -> ItemOut:
    """Add one item. Typed by the user, so it is reviewed by definition."""
    profile = await pipeline.ensure_profile(session, user_id=identity.user_id)
    highest = (
        await session.execute(
            select(ProfileItem.display_order)
            .where(ProfileItem.profile_id == profile.id)
            .order_by(ProfileItem.display_order.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    item = ProfileItem(
        user_id=identity.user_id,
        profile_id=profile.id,
        display_order=(highest or 0) + 1,
        reviewed=True,
        **body.model_dump(),
    )
    session.add(item)
    await session.flush()
    await pipeline.embed_items(session, user_id=identity.user_id, client=runtime.llm)
    await session.refresh(item)
    return _item(item)


@router.patch(
    "/items/{item_id}",
    summary="Edit an item",
    responses=error_responses(UnauthenticatedError, NotFoundError),
)
async def update_item(
    item_id: uuid.UUID,
    body: ItemPatch,
    session: TenantSession,
    identity: CurrentIdentity,
    runtime: AppRuntime,
) -> ItemOut:
    """Edit an item, re-embedding it if the text changed.

    Editing is also how an imported item gets reviewed: correcting the wording is the
    review. Anything the user touches is theirs.
    """
    item = (
        await session.execute(select(ProfileItem).where(ProfileItem.id == item_id))
    ).scalar_one_or_none()
    if item is None:
        raise NotFoundError("profile item")

    changes = body.model_dump(exclude_unset=True)
    if "text" in changes and changes["text"] != item.text:
        # A stale vector would mean the score compares against wording the user has
        # already replaced — a score that will not move however the item is edited.
        await pipeline.clear_embedding(item)
    for field, value in changes.items():
        setattr(item, field, value)

    await session.flush()
    await pipeline.embed_items(session, user_id=identity.user_id, client=runtime.llm)
    await session.refresh(item)
    return _item(item)


@router.delete(
    "/items/{item_id}",
    summary="Remove an item",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(UnauthenticatedError, NotFoundError),
)
async def delete_item(item_id: uuid.UUID, session: TenantSession) -> None:
    item = (
        await session.execute(select(ProfileItem).where(ProfileItem.id == item_id))
    ).scalar_one_or_none()
    if item is None:
        raise NotFoundError("profile item")
    await session.delete(item)


@router.post(
    "/items/review-all",
    summary="Accept every imported item",
    responses=error_responses(UnauthenticatedError),
)
async def review_all(session: TenantSession, identity: CurrentIdentity) -> ProfileOut:
    """Mark every item reviewed.

    Offered because reviewing forty items one at a time is a real cost, and a user who
    trusts the import should be able to say so once. It is an explicit action, not a
    default, which keeps the meaning of `reviewed` intact.
    """
    profile = await pipeline.ensure_profile(session, user_id=identity.user_id)
    for item in await _items(session, profile.id):
        item.reviewed = True
    await session.flush()
    return _shape(profile, await _items(session, profile.id))


@router.post(
    "/import",
    summary="Import a resume PDF",
    responses=error_responses(UnauthenticatedError, UnprocessableInputError),
)
async def import_resume(
    session: TenantSession,
    identity: CurrentIdentity,
    runtime: AppRuntime,
    file: Annotated[UploadFile, File(description="A PDF with a text layer")],
) -> ImportOut:
    """Parse a resume into items for review.

    Nothing imported is trusted. Every item arrives unreviewed, is excluded from the
    fit score until accepted, and may not be cited by generated content.
    """
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise UnprocessableInputError(f"that file is over {MAX_UPLOAD_BYTES // 1_048_576} MB")

    try:
        result = await pipeline.import_resume(
            session, user_id=identity.user_id, pdf=data, client=runtime.llm
        )
    except ResumeReadError as exc:
        # These messages are written to be shown: a password-protected file, a scan
        # with no text layer, a file that is not a PDF. The user is the one who can fix
        # every one of them.
        raise UnprocessableInputError(str(exc)) from exc
    except LlmError as exc:
        raise UnprocessableInputError("the resume could not be parsed. Try again.") from exc

    # Stored after parsing, so a file that cannot be read is not kept.
    await runtime.uploads.put(
        f"{identity.user_id}/resumes/{result.profile_id}.pdf",
        data,
        content_type="application/pdf",
    )

    return ImportOut(
        imported=result.imported, headline=result.headline, needs_review=result.imported
    )


async def _items(session: TenantSession, profile_id: uuid.UUID) -> list[ProfileItem]:
    return list(
        (
            await session.execute(
                select(ProfileItem)
                .where(ProfileItem.profile_id == profile_id)
                .order_by(ProfileItem.display_order)
            )
        )
        .scalars()
        .all()
    )


def _item(item: ProfileItem) -> ItemOut:
    return ItemOut(
        id=item.id,
        text=item.text,
        kind=item.kind,
        organisation=item.organisation,
        role=item.role,
        started_on=item.started_on,
        ended_on=item.ended_on,
        display_order=item.display_order,
        reviewed=item.reviewed,
        embedded=item.embedding is not None,
    )


def _shape(profile: Profile, items: list[ProfileItem]) -> ProfileOut:
    shaped = [_item(item) for item in items]
    return ProfileOut(
        id=profile.id,
        updated_at=profile.updated_at,
        headline=profile.headline,
        summary=profile.summary,
        full_name=profile.full_name,
        contact_email=profile.contact_email,
        phone=profile.phone,
        location=profile.location,
        links=profile.links,
        items=shaped,
        reviewed_count=sum(1 for item in shaped if item.reviewed),
        unreviewed_count=sum(1 for item in shaped if not item.reviewed),
    )
