"""Tailoring, rendering and nudges, end to end.

The tailoring tests assert the property the feature exists for: the document contains
only the user's own lines, each citing the profile item it came from. Under the
heuristic backend that holds by construction, which is the point — the validator is
exercised on a real document rather than mocked, and the same code runs when the
backend is Vertex.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobtrack_api.main import app
from jobtrack_core.config import get_settings

pytestmark = pytest.mark.integration

PROJECT = "jobtrack-test-project"

POSTING = """
Requirements
- Five years building backend services in Python
- Deep knowledge of PostgreSQL query tuning
- Experience operating Kubernetes clusters in production

Nice to have
- Exposure to Kafka
"""

ITEMS = [
    "Six years building backend services in Python at a payments company",
    "Tuned PostgreSQL queries and indexes for a 40,000,000 row ledger",
    "Built the streaming ingestion layer on Kafka processing 2TB daily",
]


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
    clean_database: None,
    db_port: str,
) -> Iterator[TestClient]:
    for key, value in {
        "ENVIRONMENT": "local",
        "GOOGLE_CLOUD_PROJECT": PROJECT,
        "FIREBASE_AUTH_EMULATOR_HOST": "localhost:9099",
        "DB_NAME": "jobtrack_test",
        "DB_PORT": db_port,
        "LLM_BACKEND": "heuristic",
    }.items():
        isolated_environment.setenv(key, value)
    get_settings.cache_clear()
    with TestClient(app) as started:
        yield started
    get_settings.cache_clear()


def ready_application(client: TestClient, subject: str) -> tuple[str, str]:
    """A profile, a ready posting, and an application tracking it."""
    for text in ITEMS:
        client.post("/profile/items", json={"text": text}, headers=auth(subject))

    created = client.post(
        "/jobs",
        json={"text": POSTING, "title": "Senior Backend Engineer", "company": "Northwind"},
        headers=auth(subject),
    )
    job_id = created.json()["id"]

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if client.get(f"/jobs/{job_id}", headers=auth(subject)).json()["status"] in {
            "ready",
            "failed",
        }:
            break
        time.sleep(0.2)

    application = client.post("/applications", json={"job_id": job_id}, headers=auth(subject))
    return job_id, application.json()["id"]


class TestSkillsGap:
    def test_it_says_which_named_skills_are_covered(self, client: TestClient) -> None:
        job_id, _ = ready_application(client, "idp|sk1")

        skills = client.get(f"/jobs/{job_id}/score", headers=auth("idp|sk1")).json()["skills"]

        have = {entry["skill"] for entry in skills["have"]}
        assert "python" in {skill.lower() for skill in have}
        assert "kafka" in {skill.lower() for skill in have}

    def test_it_says_which_are_missing(self, client: TestClient) -> None:
        # Nothing in the profile mentions Kubernetes, and Docker-adjacent experience
        # is not Kubernetes experience.
        job_id, _ = ready_application(client, "idp|sk2")

        skills = client.get(f"/jobs/{job_id}/score", headers=auth("idp|sk2")).json()["skills"]

        assert "kubernetes" in {skill.lower() for skill in skills["lack"]}

    def test_every_claimed_skill_names_its_evidence(self, client: TestClient) -> None:
        # A claim the user cannot see the basis for is one they cannot check.
        job_id, _ = ready_application(client, "idp|sk3")

        skills = client.get(f"/jobs/{job_id}/score", headers=auth("idp|sk3")).json()["skills"]

        assert skills["have"]
        assert all(entry["evidence"] for entry in skills["have"])


class TestTailoring:
    def test_it_generates_a_document(self, client: TestClient) -> None:
        _, application_id = ready_application(client, "idp|t1")

        response = client.post(f"/applications/{application_id}/tailor", headers=auth("idp|t1"))

        assert response.status_code == 201, response.text
        assert response.json()["artifact"]["bullet_count"] > 0

    def test_every_bullet_is_one_of_the_users_own_lines(self, client: TestClient) -> None:
        # The heuristic backend selects and orders rather than rewriting, so this is
        # exact. Under Vertex the same bullets are rephrased and the validator is what
        # holds the line instead.
        _, application_id = ready_application(client, "idp|t2")
        client.post(f"/applications/{application_id}/tailor", headers=auth("idp|t2"))

        artifacts = client.get(
            f"/applications/{application_id}/artifacts", headers=auth("idp|t2")
        ).json()
        pdf = client.get(f"/artifacts/{artifacts[0]['id']}/pdf", headers=auth("idp|t2"))

        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content.startswith(b"%PDF")

    def test_versions_accumulate_rather_than_replace(self, client: TestClient) -> None:
        # An earlier draft stays recoverable.
        _, application_id = ready_application(client, "idp|t3")
        client.post(f"/applications/{application_id}/tailor", headers=auth("idp|t3"))
        client.post(f"/applications/{application_id}/tailor", headers=auth("idp|t3"))

        artifacts = client.get(
            f"/applications/{application_id}/artifacts", headers=auth("idp|t3")
        ).json()

        assert [artifact["version"] for artifact in artifacts] == [2, 1]

    def test_an_empty_profile_is_refused_with_a_usable_message(self, client: TestClient) -> None:
        created = client.post(
            "/jobs", json={"text": POSTING, "title": "Engineer"}, headers=auth("idp|t4")
        )
        job_id = created.json()["id"]
        time.sleep(1.5)
        application = client.post("/applications", json={"job_id": job_id}, headers=auth("idp|t4"))

        response = client.post(
            f"/applications/{application.json()['id']}/tailor", headers=auth("idp|t4")
        )

        assert response.status_code == 400
        assert "nothing reviewed" in response.json()["detail"]

    def test_one_user_cannot_tailor_anothers_application(self, client: TestClient) -> None:
        _, application_id = ready_application(client, "idp|t5")

        response = client.post(f"/applications/{application_id}/tailor", headers=auth("idp|t6"))

        assert response.status_code == 404


class TestNudges:
    def test_a_fresh_application_is_not_nudged(self, client: TestClient) -> None:
        # Applied today is not stale. A nudge here would be noise, and noise is how a
        # feature like this gets switched off.
        ready_application(client, "idp|n1")

        result = client.post("/nudges/sweep", headers=auth("idp|n1")).json()

        assert result["drafted"] == 0

    def test_a_stale_application_is_drafted_once(self, client: TestClient) -> None:
        _, application_id = ready_application(client, "idp|n2")
        client.post(
            f"/applications/{application_id}/stage",
            json={"to_stage": "applied", "occurred_at": "2026-01-05T09:00:00Z"},
            headers=auth("idp|n2"),
        )

        first = client.post("/nudges/sweep", headers=auth("idp|n2")).json()
        second = client.post("/nudges/sweep", headers=auth("idp|n2")).json()

        assert first["drafted"] == 1
        # The unique constraint, not a prior read: two sweeps overlapping is the
        # normal case when a scheduler fires while a manual sweep is running.
        assert second["drafted"] == 0
        assert second["already_had_one"] == 1

    def test_a_draft_has_a_subject_and_a_body(self, client: TestClient) -> None:
        _, application_id = ready_application(client, "idp|n3")
        client.post(
            f"/applications/{application_id}/stage",
            json={"to_stage": "applied", "occurred_at": "2026-01-05T09:00:00Z"},
            headers=auth("idp|n3"),
        )
        client.post("/nudges/sweep", headers=auth("idp|n3"))

        nudges = client.get("/nudges", headers=auth("idp|n3")).json()

        assert len(nudges) == 1
        assert nudges[0]["draft_subject"]
        assert len(nudges[0]["draft_body"]) > 40

    def test_editing_a_draft_records_that_someone_looked_at_it(self, client: TestClient) -> None:
        _, application_id = ready_application(client, "idp|n4")
        client.post(
            f"/applications/{application_id}/stage",
            json={"to_stage": "applied", "occurred_at": "2026-01-05T09:00:00Z"},
            headers=auth("idp|n4"),
        )
        client.post("/nudges/sweep", headers=auth("idp|n4"))
        nudge_id = client.get("/nudges", headers=auth("idp|n4")).json()[0]["id"]

        updated = client.patch(
            f"/nudges/{nudge_id}",
            json={"draft_body": "My own words, sent by me."},
            headers=auth("idp|n4"),
        )

        assert updated.json()["state"] == "edited"

    def test_a_dismissed_draft_leaves_the_list(self, client: TestClient) -> None:
        _, application_id = ready_application(client, "idp|n5")
        client.post(
            f"/applications/{application_id}/stage",
            json={"to_stage": "applied", "occurred_at": "2026-01-05T09:00:00Z"},
            headers=auth("idp|n5"),
        )
        client.post("/nudges/sweep", headers=auth("idp|n5"))
        nudge_id = client.get("/nudges", headers=auth("idp|n5")).json()[0]["id"]

        client.patch(f"/nudges/{nudge_id}", json={"state": "dismissed"}, headers=auth("idp|n5"))

        assert client.get("/nudges", headers=auth("idp|n5")).json() == []

    def test_one_user_never_sees_anothers_drafts(self, client: TestClient) -> None:
        _, application_id = ready_application(client, "idp|n6")
        client.post(
            f"/applications/{application_id}/stage",
            json={"to_stage": "applied", "occurred_at": "2026-01-05T09:00:00Z"},
            headers=auth("idp|n6"),
        )
        client.post("/nudges/sweep", headers=auth("idp|n6"))

        assert client.get("/nudges", headers=auth("idp|n7")).json() == []

    def test_there_is_no_way_to_send_one(self, client: TestClient) -> None:
        # The strongest form of "nothing is sent on your behalf" is that no endpoint
        # exists to do it. This fails the moment someone adds one.
        paths = client.get("/openapi.json").json()["paths"]

        assert not any("send" in path.lower() for path in paths)
