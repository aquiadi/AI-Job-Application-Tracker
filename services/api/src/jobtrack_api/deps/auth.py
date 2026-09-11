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
from sqlalchemy.ext.asyncio import AsyncSession

from jobtrack_api.errors import UnauthenticatedError
from jobtrack_core.auth import FirebaseTokenVerifier, InvalidTokenError, VerifiedIdentity
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
    everything it did — including, once there is one, the outbox row that would have
    announced a change that did not happen.
    """
    async with tenant_session(database.sessions, identity.user_id) as session:
        yield session


CurrentIdentity = Annotated[VerifiedIdentity, Depends(current_identity)]
TenantSession = Annotated[AsyncSession, Depends(tenant_db)]
