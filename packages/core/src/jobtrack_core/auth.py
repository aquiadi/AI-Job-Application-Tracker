"""Identity Platform ID token verification.

Every request to the api carries a Firebase ID token, and this is the only place it is
trusted. Two things are worth knowing before changing anything here.

**`google.auth` does not check the issuer.** ``jwt.decode`` verifies the signature, the
expiry and the audience, and stops there. A token minted by a *different* Firebase
project carries a valid Google signature and would pass, so the issuer is checked
explicitly below. That check is the difference between "signed by Google" and "issued
for this project".

**The user id is derived from the subject, not looked up.** A tenant-scoped query needs
the internal user id before it can set the row-level security context, but finding that
id by subject would itself be a query against a tenant-scoped table — a chicken and egg
that is usually resolved with a privileged lookup path that bypasses RLS. Deriving the
id as a UUID5 of the subject removes the problem instead of working around it: the
tenant context is computable from the token alone, and there is no code path anywhere
that reads user rows without a tenant set.

The trade is that the id is a pure function of the identity-provider subject, so anyone
holding the subject can compute it. Ids are not secrets here — the policies, not the
unguessability of a primary key, are what protect a row.
"""

from __future__ import annotations

import asyncio
import binascii
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from base64 import urlsafe_b64decode
from dataclasses import dataclass
from typing import Any, Final

from google.auth import jwt

#: Public keys Google signs Identity Platform ID tokens with.
CERTS_URL: Final = (
    "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
)

#: Namespace for deriving internal user ids. Fixed forever: changing it orphans every
#: existing row, because the derived id is the tenant key.
USER_ID_NAMESPACE: Final = uuid.UUID("6f8a1c2e-5b47-5d93-9a1f-2c7e4d8b3a60")

#: Fallback when Google's response carries no usable Cache-Control.
_DEFAULT_CERT_TTL_SECONDS: Final = 3600
_MAX_AGE = re.compile(r"max-age=(\d+)")


class InvalidTokenError(Exception):
    """The token is absent, malformed, expired, or not for this project.

    Deliberately one exception with a short reason rather than a hierarchy. The api
    returns 401 for all of them and tells the client nothing beyond that; a caller
    learning *why* their forged token failed is a small gift to an attacker.
    """


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """A caller whose token has been checked."""

    subject: str
    email: str | None
    user_id: uuid.UUID
    email_verified: bool


def derive_user_id(subject: str) -> uuid.UUID:
    """Map an Identity Platform subject to this system's user id.

    Deterministic, so the same person always resolves to the same tenant without a
    lookup. See the module docstring for why that matters.
    """
    if not subject:
        raise InvalidTokenError("token has no subject")
    return uuid.uuid5(USER_ID_NAMESPACE, subject)


def _b64_segment(segment: str) -> dict[str, Any]:
    """Decode one base64url JWT segment into a JSON object."""
    padded = segment + "=" * (-len(segment) % 4)
    try:
        raw = urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise InvalidTokenError("token segment is not valid base64") from exc
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidTokenError("token segment is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise InvalidTokenError("token segment is not an object")
    return decoded


def _split(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    parts = token.split(".")
    if len(parts) != 3:
        raise InvalidTokenError("token is not a JWT")
    return _b64_segment(parts[0]), _b64_segment(parts[1])


class _CertCache:
    """Google's signing certificates, fetched once and reused until they expire.

    Fetching per request would put a synchronous HTTP round trip to Google in front of
    every authenticated call. The keys rotate roughly daily, and the response says how
    long it may be cached.
    """

    def __init__(self) -> None:
        self._certs: dict[str, str] = {}
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self, *, force: bool = False) -> dict[str, str]:
        async with self._lock:
            if force or not self._certs or time.time() >= self._expires_at:
                certs, ttl = await asyncio.to_thread(self._fetch)
                self._certs = certs
                self._expires_at = time.time() + ttl
            return self._certs

    @staticmethod
    def _fetch() -> tuple[dict[str, str], int]:
        request = urllib.request.Request(CERTS_URL, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                payload = json.loads(response.read())
                cache_control = response.headers.get("Cache-Control", "")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise InvalidTokenError("could not fetch signing certificates") from exc

        match = _MAX_AGE.search(cache_control)
        ttl = int(match.group(1)) if match else _DEFAULT_CERT_TTL_SECONDS
        return payload, max(ttl, 60)


class FirebaseTokenVerifier:
    """Verifies Identity Platform ID tokens for one project."""

    def __init__(
        self,
        project_id: str,
        *,
        emulator_host: str | None = None,
        clock_skew_seconds: int = 10,
    ) -> None:
        if not project_id:
            raise ValueError("project_id is required to verify ID tokens")
        self._project_id = project_id
        self._emulator_host = emulator_host or None
        self._clock_skew = clock_skew_seconds
        self._certs = _CertCache()

    @property
    def expected_issuer(self) -> str:
        return f"https://securetoken.google.com/{self._project_id}"

    async def verify(self, token: str) -> VerifiedIdentity:
        """Check a token and return who it belongs to.

        Raises:
            InvalidTokenError: for anything that is not a valid, current token issued
                by this project.
        """
        if not token:
            raise InvalidTokenError("no token supplied")

        claims = (
            self._decode_emulator(token)
            if self._emulator_host
            else await self._decode_signed(token)
        )
        self._check_claims(claims)

        subject = str(claims.get("sub", ""))
        email = claims.get("email")
        return VerifiedIdentity(
            subject=subject,
            email=str(email) if email else None,
            user_id=derive_user_id(subject),
            email_verified=bool(claims.get("email_verified", False)),
        )

    async def _decode_signed(self, token: str) -> dict[str, Any]:
        """Verify the signature against Google's current public keys."""
        header, _ = _split(token)

        algorithm = header.get("alg")
        if algorithm != "RS256":
            # An unsigned token is exactly what an attacker submits first.
            raise InvalidTokenError(f"unexpected signing algorithm: {algorithm!r}")

        certs = await self._certs.get()
        kid = header.get("kid")
        if kid is not None and kid not in certs:
            # Keys rotate. One forced refresh distinguishes a rotation from a forgery.
            certs = await self._certs.get(force=True)

        try:
            claims = jwt.decode(token, certs=certs, audience=self._project_id)
        except ValueError as exc:
            raise InvalidTokenError("token failed verification") from exc
        return dict(claims)

    def _decode_emulator(self, token: str) -> dict[str, Any]:
        """Read a token from the Firebase Auth emulator, which does not sign.

        The emulator issues tokens with ``alg: none`` and an empty signature, so there
        is nothing to verify cryptographically. Every other check still runs, and this
        path is unreachable unless `FIREBASE_AUTH_EMULATOR_HOST` is set — which
        `Settings` only permits outside cloud.
        """
        _, payload = _split(token)
        return payload

    def _check_claims(self, claims: dict[str, Any]) -> None:
        """Apply the checks `jwt.decode` does not, and re-apply those it skips locally."""
        if claims.get("iss") != self.expected_issuer:
            # A token from another Firebase project carries a valid Google signature.
            raise InvalidTokenError("token was not issued for this project")

        if claims.get("aud") != self._project_id:
            raise InvalidTokenError("token audience does not match this project")

        if not claims.get("sub"):
            raise InvalidTokenError("token has no subject")

        now = time.time()
        expiry = claims.get("exp")
        if not isinstance(expiry, int | float) or now > float(expiry) + self._clock_skew:
            raise InvalidTokenError("token has expired")

        issued_at = claims.get("iat")
        if isinstance(issued_at, int | float) and float(issued_at) > now + self._clock_skew:
            raise InvalidTokenError("token was issued in the future")
