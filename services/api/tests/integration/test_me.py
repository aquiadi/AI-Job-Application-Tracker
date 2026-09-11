"""The whole M1 chain through a real HTTP call.

Token in, identity derived, user row provisioned under row-level security, account
returned. The unit tests prove each link; this proves they are actually connected, and
that a request with no token or someone else's token gets nothing.

Runs against the Auth emulator's unsigned-token format, so it needs no credentials and
no network.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobtrack_api.main import app
from jobtrack_core.auth import derive_user_id
from jobtrack_core.config import get_settings

pytestmark = pytest.mark.integration

PROJECT = "jobtrack-test-project"
TEST_DB = "jobtrack_test"


def _token(subject: str, email: str = "person@example.test", **overrides: Any) -> str:
    """An emulator-format ID token: unsigned, but otherwise shaped like a real one."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "aud": PROJECT,
        "sub": subject,
        "email": email,
        "email_verified": True,
        "iat": now - 5,
        "exp": now + 3600,
    }
    claims.update(overrides)

    def segment(payload: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'none', 'typ': 'JWT'})}.{segment(claims)}."


def _auth(subject: str, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(subject, **kwargs)}"}


@pytest.fixture
def client(
    isolated_environment: pytest.MonkeyPatch, migrated_database: None, db_port: str
) -> Iterator[TestClient]:
    for key, value in {
        "ENVIRONMENT": "local",
        "GOOGLE_CLOUD_PROJECT": PROJECT,
        "FIREBASE_AUTH_EMULATOR_HOST": "localhost:9099",
        "DB_NAME": TEST_DB,
        "DB_PORT": db_port,
    }.items():
        isolated_environment.setenv(key, value)

    get_settings.cache_clear()
    with TestClient(app) as started:
        yield started
    get_settings.cache_clear()


class TestAuthentication:
    def test_a_request_with_no_token_is_refused(self, client: TestClient) -> None:
        response = client.get("/me")

        assert response.status_code == 401
        assert response.json()["code"] == "unauthenticated"
        # RFC 9110 requires this on a 401, and it is what tells a client to
        # re-authenticate rather than retry.
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_a_token_from_another_project_is_refused(self, client: TestClient) -> None:
        response = client.get(
            "/me",
            headers={
                "Authorization": "Bearer "
                + _token("someone", iss="https://securetoken.google.com/other-project")
            },
        )

        assert response.status_code == 401

    def test_the_reason_for_a_rejection_is_not_disclosed(self, client: TestClient) -> None:
        # Every failure reads the same from outside. A caller learning why their
        # forged token failed is being helped to forge a better one.
        expired = client.get("/me", headers=_auth("someone", exp=int(time.time()) - 10))
        garbage = client.get("/me", headers={"Authorization": "Bearer not-a-jwt"})

        assert (
            expired.json()
            == garbage.json()
            == {
                "code": "unauthenticated",
                "detail": "invalid token",
            }
        )


class TestProvisioning:
    def test_the_first_request_creates_the_account(self, client: TestClient) -> None:
        response = client.get("/me", headers=_auth("idp|first-timer"))

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(derive_user_id("idp|first-timer"))
        assert body["email"] == "person@example.test"
        assert body["has_profile"] is False

    def test_provisioning_is_idempotent(self, client: TestClient) -> None:
        # A browser loading the app shell and its first data call together produces
        # two of these at once on a brand new account.
        first = client.get("/me", headers=_auth("idp|repeat"))
        second = client.get("/me", headers=_auth("idp|repeat"))

        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["created_at"] == second.json()["created_at"]

    def test_two_subjects_get_two_accounts(self, client: TestClient) -> None:
        alice = client.get("/me", headers=_auth("idp|alice", email="alice@example.test"))
        bob = client.get("/me", headers=_auth("idp|bob", email="bob@example.test"))

        assert alice.json()["id"] != bob.json()["id"]
        assert alice.json()["email"] == "alice@example.test"
        assert bob.json()["email"] == "bob@example.test"

    def test_the_account_id_is_derived_from_the_subject(self, client: TestClient) -> None:
        # Not an implementation detail: it is what lets the tenant context be set
        # before any row is read, so there is no privileged lookup anywhere.
        response = client.get("/me", headers=_auth("idp|derived"))

        assert uuid.UUID(response.json()["id"]) == derive_user_id("idp|derived")

    def test_a_users_email_follows_their_token(self, client: TestClient) -> None:
        created = client.get("/me", headers=_auth("idp|stable", email="original@example.test"))
        assert created.json()["email"] == "original@example.test"
