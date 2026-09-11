"""The signed-in user.

`GET /me` doubles as provisioning. There is no separate sign-up call, because there is
nothing to sign up *with*: Identity Platform has already established who the person is
by the time the first request arrives, and a second step would only be somewhere for
the two records to drift apart.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from jobtrack_api.deps.auth import CurrentIdentity, TenantSession
from jobtrack_core.db.models import Profile, User

router = APIRouter(tags=["me"])


class Me(BaseModel):
    """The caller's account."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    timezone: str
    created_at: datetime
    has_profile: bool


@router.get("/me", summary="The signed-in user", status_code=status.HTTP_200_OK)
async def read_me(identity: CurrentIdentity, session: TenantSession) -> Me:
    """Return the caller's account.

    The row is created by the `tenant_db` dependency, on whichever request arrives
    first. What makes that possible is that the id is derived from the token subject,
    so it is known before the row exists — looking the user up by subject would need a
    query the row-level security policy on `users` correctly refuses, and the usual
    escape from that is a privileged lookup path that bypasses RLS. There isn't one.
    """
    user = (await session.execute(select(User).where(User.id == identity.user_id))).scalar_one()
    profile_id = (
        await session.execute(select(Profile.id).where(Profile.user_id == identity.user_id))
    ).scalar_one_or_none()

    return Me(
        id=user.id,
        email=user.email,
        timezone=user.timezone,
        created_at=user.created_at,
        has_profile=profile_id is not None,
    )
