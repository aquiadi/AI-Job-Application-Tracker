"""Token verification, including the forgeries it has to refuse.

Tests that only check the happy path would pass against a verifier that accepted
everything. Most of what is below is an attack that has to fail.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import crypt, jwt

from jobtrack_core.auth import (
    USER_ID_NAMESPACE,
    FirebaseTokenVerifier,
    InvalidTokenError,
    derive_user_id,
)

PROJECT = "jobtrack-test-project"
ISSUER = f"https://securetoken.google.com/{PROJECT}"
KID = "test-key-1"


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    """A private key to sign test tokens with, and the public key to verify them."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


@pytest.fixture
def verifier(keypair: tuple[str, str]) -> FirebaseTokenVerifier:
    """A verifier whose certificate cache is pre-loaded, so no network call happens."""
    _, public = keypair
    subject = FirebaseTokenVerifier(PROJECT)
    subject._certs._certs = {KID: public}
    subject._certs._expires_at = time.time() + 3600
    return subject


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "aud": PROJECT,
        "sub": "idp-subject-abc",
        "email": "person@example.test",
        "email_verified": True,
        "iat": now - 5,
        "exp": now + 3600,
    }
    base.update(overrides)
    return base


def _sign(private_key: str, claims: dict[str, Any]) -> str:
    # google-auth ships py.typed but leaves these two unannotated, so strict mode
    # rejects the calls rather than the arguments.
    signer = crypt.RSASigner.from_string(private_key, key_id=KID)  # type: ignore[no-untyped-call]
    token: bytes = jwt.encode(signer, claims)  # type: ignore[no-untyped-call]
    return token.decode()


def _unsigned(claims: dict[str, Any], algorithm: str = "none") -> str:
    """Build an emulator-style token: no signature, and none expected."""

    def segment(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{segment({'alg': algorithm, 'typ': 'JWT'})}.{segment(claims)}."


class TestDerivedUserIds:
    def test_the_same_subject_always_gives_the_same_id(self) -> None:
        # This is what removes the need for a privileged lookup: the tenant key is
        # computable from the token alone.
        assert derive_user_id("idp|alice") == derive_user_id("idp|alice")

    def test_different_subjects_give_different_ids(self) -> None:
        assert derive_user_id("idp|alice") != derive_user_id("idp|bob")

    def test_the_id_is_a_uuid5_of_the_subject(self) -> None:
        assert derive_user_id("idp|alice") == uuid.uuid5(USER_ID_NAMESPACE, "idp|alice")

    def test_an_empty_subject_is_refused(self) -> None:
        with pytest.raises(InvalidTokenError, match="subject"):
            derive_user_id("")


class TestValidTokens:
    async def test_a_well_formed_token_verifies(
        self, verifier: FirebaseTokenVerifier, keypair: tuple[str, str]
    ) -> None:
        private, _ = keypair
        identity = await verifier.verify(_sign(private, _claims()))

        assert identity.subject == "idp-subject-abc"
        assert identity.email == "person@example.test"
        assert identity.email_verified is True
        assert identity.user_id == derive_user_id("idp-subject-abc")

    async def test_a_token_without_an_email_still_verifies(
        self, verifier: FirebaseTokenVerifier, keypair: tuple[str, str]
    ) -> None:
        # Some providers return no email. That is an identity without a contact
        # address, not an invalid one.
        private, _ = keypair
        claims = _claims()
        del claims["email"]

        identity = await verifier.verify(_sign(private, claims))
        assert identity.email is None


class TestForgeries:
    async def test_a_token_from_another_firebase_project_is_refused(
        self, verifier: FirebaseTokenVerifier, keypair: tuple[str, str]
    ) -> None:
        # The one google.auth does not catch on its own. A token minted by a different
        # project carries a genuine Google signature.
        private, _ = keypair
        token = _sign(private, _claims(iss="https://securetoken.google.com/some-other-project"))

        with pytest.raises(InvalidTokenError, match="not issued for this project"):
            await verifier.verify(token)

    async def test_a_token_for_another_audience_is_refused(
        self, verifier: FirebaseTokenVerifier, keypair: tuple[str, str]
    ) -> None:
        private, _ = keypair
        with pytest.raises(InvalidTokenError):
            await verifier.verify(_sign(private, _claims(aud="someone-elses-project")))

    async def test_an_unsigned_token_is_refused_outside_the_emulator(
        self, verifier: FirebaseTokenVerifier
    ) -> None:
        # The first thing anyone tries: strip the signature and set alg to none.
        with pytest.raises(InvalidTokenError, match="signing algorithm"):
            await verifier.verify(_unsigned(_claims()))

    async def test_a_token_signed_with_the_wrong_key_is_refused(
        self, verifier: FirebaseTokenVerifier
    ) -> None:
        attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        attacker_pem = attacker.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

        with pytest.raises(InvalidTokenError):
            await verifier.verify(_sign(attacker_pem, _claims()))

    async def test_an_expired_token_is_refused(
        self, verifier: FirebaseTokenVerifier, keypair: tuple[str, str]
    ) -> None:
        private, _ = keypair
        now = int(time.time())
        with pytest.raises(InvalidTokenError):
            await verifier.verify(_sign(private, _claims(iat=now - 7200, exp=now - 3600)))

    @pytest.mark.parametrize(
        "garbage", ["", "not-a-jwt", "a.b", "a.b.c.d", "!!!.???.***", "eyJhbGciOiJub25lIn0"]
    )
    async def test_malformed_input_is_refused_without_raising_anything_else(
        self, verifier: FirebaseTokenVerifier, garbage: str
    ) -> None:
        # Everything reaching this function is attacker-controlled, so the only
        # acceptable failure is InvalidTokenError.
        with pytest.raises(InvalidTokenError):
            await verifier.verify(garbage)


class TestEmulatorMode:
    @pytest.fixture
    def emulator_verifier(self) -> FirebaseTokenVerifier:
        return FirebaseTokenVerifier(PROJECT, emulator_host="localhost:9099")

    async def test_an_unsigned_emulator_token_verifies(
        self, emulator_verifier: FirebaseTokenVerifier
    ) -> None:
        identity = await emulator_verifier.verify(_unsigned(_claims()))
        assert identity.subject == "idp-subject-abc"

    async def test_the_issuer_is_still_checked_locally(
        self, emulator_verifier: FirebaseTokenVerifier
    ) -> None:
        # Skipping the signature is not a licence to skip everything else, or local
        # development stops resembling production in the way that matters.
        with pytest.raises(InvalidTokenError, match="not issued for this project"):
            await emulator_verifier.verify(_unsigned(_claims(iss="https://evil.test/x")))

    async def test_expiry_is_still_checked_locally(
        self, emulator_verifier: FirebaseTokenVerifier
    ) -> None:
        now = int(time.time())
        with pytest.raises(InvalidTokenError, match="expired"):
            await emulator_verifier.verify(_unsigned(_claims(iat=now - 7200, exp=now - 3600)))

    async def test_a_subjectless_token_is_refused(
        self, emulator_verifier: FirebaseTokenVerifier
    ) -> None:
        with pytest.raises(InvalidTokenError, match="subject"):
            await emulator_verifier.verify(_unsigned(_claims(sub="")))


class TestConstruction:
    def test_a_verifier_needs_a_project(self) -> None:
        # An empty project id would make the audience check compare against "" and
        # accept tokens from anywhere.
        with pytest.raises(ValueError, match="project_id"):
            FirebaseTokenVerifier("")

    def test_the_expected_issuer_is_derived_from_the_project(self) -> None:
        assert FirebaseTokenVerifier("abc").expected_issuer == (
            "https://securetoken.google.com/abc"
        )
