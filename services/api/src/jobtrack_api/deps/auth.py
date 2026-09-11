"""Request authentication and the tenant-scoped session that follows from it.

The chain is deliberately short and has no branches: a bearer token is verified, the
tenant key is derived from it, and every database session opened for that request is
scoped to that key. There is no code path that reaches user data without going through
:func:`tenant_db`, and no way to influence which tenant a request reads by sending a
header or a body field.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobtrack_api.errors import UnauthenticatedError
from jobtrack_core.auth import FirebaseTokenVerifier, InvalidTokenError, VerifiedIdentity
from jobtrack_core.db.models import User
from jobtrack_core.db.session import Database, tenant_session

# auto_error=False so a missing header produces our own 401 with a WWW-Authenticate
# challenge, rather than FastAPI's 403, which is the wrong status for "no credentials".
_bearer = HTTPBearer(auto_error=False, scheme_name="Identity Platform ID token")


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_verifier(request: Request) -> FirebaseTokenVerifier:
    verifier: FirebaseTokenVerifier = request.app.state.verifier
    return verifier


async def current_identity(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    verifier: Annotated[FirebaseTokenVerifier, Depends(get_verifier)],
) -> VerifiedIdentity:
    """Verify the bearer token and return who sent it."""
    if credentials is None or not credentials.credentials:
        raise UnauthenticatedError("missing bearer token")

    try:
        return await verifier.verify(credentials.credentials)
    except InvalidTokenError as exc:
        # The reason is logged, not returned. Telling a caller why their forged token
        # failed helps them forge a better one.
        raise UnauthenticatedError("invalid token") from exc


async def tenant_db(
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """A database session scoped to the caller, committed when the request succeeds.

    The transaction spans the whole handler, so a request that raises rolls back
    everything it did — including the outbox row that would have announced a change
    that did not happen.

    The user row is created here rather than in a sign-up route, because there is no
    sign-up: Identity Platform has already established who the person is by the time
    the first request arrives. Doing it in this dependency rather than in `GET /me`
    means it holds for whichever endpoint a client happens to call first — a browser
    that restores a saved posting before it loads the account would otherwise fail on
    a foreign key.

    The cost is one `INSERT ... ON CONFLICT DO NOTHING` per request, which is a single
    lookup on the primary key. That is worth paying to make "the user row exists"
    an invariant of being authenticated rather than a thing each route remembers.
    """
    async with tenant_session(database.sessions, identity.user_id) as session:
        await session.execute(
            insert(User)
            .values(
                id=identity.user_id,
                subject=identity.subject,
                email=identity.email or "",
            )
            .on_conflict_do_nothing(index_elements=[User.id])
        )
        yield session


CurrentIdentity = Annotated[VerifiedIdentity, Depends(current_identity)]
TenantSession = Annotated[AsyncSession, Depends(tenant_db)]
