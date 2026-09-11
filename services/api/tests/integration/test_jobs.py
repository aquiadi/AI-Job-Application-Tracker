"""Saving a posting and watching it become requirements.

This is the M2 chain end to end against a real Postgres: the request writes a job and
an outbox row in one transaction, the relay picks the row up under its own database
role, the in-process publisher calls the handler, and the handler extracts and embeds.

It runs with no credentials. The model backend is the heuristic one, which is the
point of ADR 10 — the pipeline is exercised in full, and only the quality of the
extraction differs from a run against Vertex.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobtrack_api.main import app
from jobtrack_core.config import get_settings

pytestmark = pytest.mark.integration

PROJECT = "jobtrack-test-project"
TEST_DB = "jobtrack_test"

POSTING = """
About the role

Northwind is building the settlement platform that moves money for merchants.

What you'll do
- Own the ledger service end to end
- Partner with compliance on reconciliation

Requirements
- 5+ years building backend services in Python or Go
- Experience with distributed transactions and idempotent processing
- Operating services on Kubernetes in production

Nice to have
- Event streaming with Kafka or Pub/Sub
- Authored reusable Terraform modules

Benefits
- Private medical cover
"""


def _token(subject: str) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "aud": PROJECT,
        "sub": subject,
        "email": f"{subject}@example.test",
        "iat": now - 5,
        "exp": now + 3600,
    }

    def segment(payload: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'none', 'typ': 'JWT'})}.{segment(claims)}."


def auth(subject: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(subject)}"}


@pytest.fixture
def client(
    isolated_environment: pytest.MonkeyPatch,
    migrated_database: None,
    db_port: str,
    tmp_path: Path,
) -> Iterator[TestClient]:
    for key, value in {
        "ENVIRONMENT": "local",
        "GOOGLE_CLOUD_PROJECT": PROJECT,
        "FIREBASE_AUTH_EMULATOR_HOST": "localhost:9099",
        "DB_NAME": TEST_DB,
        "DB_PORT": db_port,
        "LLM_BACKEND": "heuristic",
    }.items():
        isolated_environment.setenv(key, value)

    get_settings.cache_clear()
    with TestClient(app) as started:
        yield started
    get_settings.cache_clear()


def wait_for_status(
    client: TestClient, job_id: str, subject: str, target: str, timeout: float = 15.0
) -> dict[str, Any]:
    """Poll until the pipeline reaches `target`.

    Polling rather than reaching into the publisher: the test asserts on what a caller
    of the API can actually observe, which is the job's status changing on its own.
    """
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get(f"/jobs/{job_id}", headers=auth(subject)).json()
        if body["status"] in {target, "failed"}:
            return body
        time.sleep(0.2)
    return body


class TestSavingAPosting:
    def test_pasted_text_becomes_requirements(self, client: TestClient) -> None:
        created = client.post("/jobs", json={"text": POSTING}, headers=auth("idp|paster"))

        assert created.status_code == 201
        body = created.json()
        assert body["created"] is True
        # Queued, not ready: extraction genuinely has not run yet, and saying
        # otherwise would be the interface lying about work in progress.
        assert body["status"] == "queued"

        final = wait_for_status(client, body["id"], "idp|paster", "ready")

        assert final["status"] == "ready", final.get("failure_reason")
        texts = {requirement["text"] for requirement in final["requirements"]}
        assert "5+ years building backend services in Python or Go" in texts
        assert "Operating services on Kubernetes in production" in texts

    def test_must_and_nice_are_separated(self, client: TestClient) -> None:
        created = client.post("/jobs", json={"text": POSTING}, headers=auth("idp|kinds"))
        final = wait_for_status(client, created.json()["id"], "idp|kinds", "ready")

        kinds = {r["text"]: r["kind"] for r in final["requirements"]}

        assert kinds["Experience with distributed transactions and idempotent processing"] == "must"
        assert kinds["Event streaming with Kafka or Pub/Sub"] == "nice"

    def test_must_haves_are_ordered_first(self, client: TestClient) -> None:
        # The board and the score both read must-haves first, so the order is set once
        # on the way in rather than re-sorted by every reader.
        created = client.post("/jobs", json={"text": POSTING}, headers=auth("idp|order"))
        final = wait_for_status(client, created.json()["id"], "idp|order", "ready")

        kinds = [requirement["kind"] for requirement in final["requirements"]]

        assert kinds == sorted(kinds, key=lambda kind: 0 if kind == "must" else 1)

    def test_requirements_are_embedded(self, client: TestClient) -> None:
        created = client.post("/jobs", json={"text": POSTING}, headers=auth("idp|embed"))
        final = wait_for_status(client, created.json()["id"], "idp|embed", "ready")

        assert all(requirement["embedded"] for requirement in final["requirements"])

    def test_benefits_do_not_become_requirements(self, client: TestClient) -> None:
        created = client.post("/jobs", json={"text": POSTING}, headers=auth("idp|benefits"))
        final = wait_for_status(client, created.json()["id"], "idp|benefits", "ready")

        texts = {requirement["text"] for requirement in final["requirements"]}

        assert "Private medical cover" not in texts

    def test_responsibilities_are_kept_apart_from_requirements(self, client: TestClient) -> None:
        created = client.post("/jobs", json={"text": POSTING}, headers=auth("idp|resp"))
        final = wait_for_status(client, created.json()["id"], "idp|resp", "ready")

        assert "Own the ledger service end to end" in final["responsibilities"]
        texts = {requirement["text"] for requirement in final["requirements"]}
        assert "Own the ledger service end to end" not in texts


class TestIdempotence:
    def test_saving_the_same_posting_twice_makes_one_job(self, client: TestClient) -> None:
        first = client.post("/jobs", json={"text": POSTING}, headers=auth("idp|twice"))
        second = client.post("/jobs", json={"text": POSTING}, headers=auth("idp|twice"))

        assert first.status_code == 201
        # 200, not 201: nothing was created, and the interface says "already saved"
        # rather than pretending otherwise.
        assert second.status_code == 200
        assert second.json()["created"] is False
        assert first.json()["id"] == second.json()["id"]

    def test_whitespace_differences_collapse_onto_one_job(self, client: TestClient) -> None:
        # The content hash is over normalised text, so a re-paste that picked up
        # different spacing is the same posting.
        first = client.post("/jobs", json={"text": POSTING}, headers=auth("idp|space"))
        respaced = POSTING.replace("- ", "•  ").replace("\n\n", "\n\n\n")
        second = client.post("/jobs", json={"text": respaced}, headers=auth("idp|space"))

        assert first.json()["id"] == second.json()["id"]

    def test_two_users_saving_one_posting_get_their_own_job(self, client: TestClient) -> None:
        alice = client.post("/jobs", json={"text": POSTING}, headers=auth("idp|alice2"))
        bob = client.post("/jobs", json={"text": POSTING}, headers=auth("idp|bob2"))

        assert alice.json()["id"] != bob.json()["id"]


class TestRejection:
    def test_an_unsupported_url_names_the_boards_that_work(self, client: TestClient) -> None:
        response = client.post(
            "/jobs", json={"url": "https://careers.example.com/jobs/1"}, headers=auth("idp|url")
        )

        assert response.status_code == 400
        assert response.json()["code"] == "unprocessable_input"
        assert "greenhouse" in response.json()["detail"]

    def test_the_metadata_endpoint_is_not_fetched(self, client: TestClient) -> None:
        # No adapter matches it, so no request is made at all. This is the request-side
        # half of the argument in router.py.
        response = client.post(
            "/jobs",
            json={"url": "http://169.254.169.254/computeMetadata/v1/"},
            headers=auth("idp|ssrf"),
        )

        assert response.status_code == 400

    def test_a_paste_too_short_to_be_a_posting_is_refused(self, client: TestClient) -> None:
        response = client.post("/jobs", json={"text": "Senior Engineer"}, headers=auth("idp|short"))

        assert response.status_code == 400
        assert "characters" in response.json()["detail"]

    def test_sending_both_url_and_text_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/jobs",
            json={"url": "https://jobs.lever.co/a/b0b0b0b0", "text": POSTING},
            headers=auth("idp|both"),
        )

        assert response.status_code == 422

    def test_an_anonymous_request_saves_nothing(self, client: TestClient) -> None:
        assert client.post("/jobs", json={"text": POSTING}).status_code == 401


class TestTenantIsolation:
    def test_one_user_cannot_read_anothers_posting(self, client: TestClient) -> None:
        created = client.post("/jobs", json={"text": POSTING}, headers=auth("idp|owner"))
        job_id = created.json()["id"]

        response = client.get(f"/jobs/{job_id}", headers=auth("idp|intruder"))

        # 404 rather than 403: a 403 would confirm the posting exists.
        assert response.status_code == 404

    def test_the_list_shows_only_your_own(self, client: TestClient) -> None:
        client.post("/jobs", json={"text": POSTING}, headers=auth("idp|mine"))

        listed = client.get("/jobs", headers=auth("idp|theirs")).json()

        assert listed == []

    def test_a_posting_that_does_not_exist_is_a_404(self, client: TestClient) -> None:
        response = client.get(f"/jobs/{uuid.uuid4()}", headers=auth("idp|missing"))

        assert response.status_code == 404


class TestRetry:
    def test_retry_requeues_the_posting(self, client: TestClient) -> None:
        created = client.post("/jobs", json={"text": POSTING}, headers=auth("idp|retry"))
        job_id = created.json()["id"]
        wait_for_status(client, job_id, "idp|retry", "ready")

        response = client.post(f"/jobs/{job_id}/retry", headers=auth("idp|retry"))

        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        # It goes back through the same path rather than being extracted inline.
        assert wait_for_status(client, job_id, "idp|retry", "ready")["status"] == "ready"


def test_asyncio_is_available() -> None:
    # The relay runs as a background task inside the app's event loop; TestClient
    # drives that loop, so this suite depends on it being started by the lifespan.
    assert asyncio.get_event_loop_policy() is not None
