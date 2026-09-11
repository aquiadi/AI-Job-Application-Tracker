"""Profile, fit score and pipeline, end to end against a real Postgres.

The score test is the one that matters most. It asserts that a requirement finds the
evidence that actually answers it and that an unanswered requirement is reported as a
gap rather than matched to the nearest thing available — which is the failure mode that
makes a fit score worthless.

It runs under the heuristic backend, so the embedding space is lexical. That limits
what these can claim: they prove the retrieval, fusion, thresholding and weighting work
on real SQL, not that the thresholds are right. Calibrating those needs real embeddings
and labelled pairs, and is what the M3 eval is for.
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
- Experience operating Kubernetes clusters in production
- Deep knowledge of PostgreSQL query tuning

Nice to have
- Exposure to Kafka
"""

ITEMS = [
    "Six years building backend services in Python at a payments company",
    "Tuned PostgreSQL queries and indexes for a 40M row ledger",
    "Wrote the reconciliation service and its test suite",
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


def ready_job(client: TestClient, subject: str, text: str = POSTING) -> str:
    created = client.post("/jobs", json={"text": text}, headers=auth(subject))
    job_id = created.json()["id"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        body = client.get(f"/jobs/{job_id}", headers=auth(subject)).json()
        if body["status"] in {"ready", "failed"}:
            break
        time.sleep(0.2)
    return str(job_id)


def seed_profile(client: TestClient, subject: str, items: list[str] = ITEMS) -> None:
    for text in items:
        response = client.post(
            "/profile/items",
            json={"text": text, "kind": "experience_bullet"},
            headers=auth(subject),
        )
        assert response.status_code == 201, response.text


class TestProfile:
    def test_a_new_user_has_an_empty_profile(self, client: TestClient) -> None:
        body = client.get("/profile", headers=auth("idp|p1")).json()

        assert body["items"] == []
        assert body["reviewed_count"] == 0

    def test_a_typed_item_is_reviewed_and_embedded(self, client: TestClient) -> None:
        # Typed by the user, so it is reviewed by definition. Embedded synchronously
        # because the user is looking at the screen.
        response = client.post("/profile/items", json={"text": ITEMS[0]}, headers=auth("idp|p2"))

        assert response.status_code == 201
        assert response.json()["reviewed"] is True
        assert response.json()["embedded"] is True

    def test_editing_the_text_re_embeds(self, client: TestClient) -> None:
        # A stale vector would mean the score compares against wording the user has
        # already replaced, and the score would not move however they edited.
        created = client.post(
            "/profile/items", json={"text": "Wrote Python services"}, headers=auth("idp|p3")
        ).json()

        edited = client.patch(
            f"/profile/items/{created['id']}",
            json={"text": "Wrote Go services"},
            headers=auth("idp|p3"),
        )

        assert edited.json()["text"] == "Wrote Go services"
        assert edited.json()["embedded"] is True

    def test_a_two_letter_skill_can_be_added(self, client: TestClient) -> None:
        # "Go", "R" and "C#" are exactly the tokens a posting screens on. A blanket
        # three-character minimum refused them, which meant a user could not enter
        # the most specific things they know.
        response = client.post(
            "/profile/items", json={"text": "Go", "kind": "skill"}, headers=auth("idp|p2b")
        )

        assert response.status_code == 201, response.text
        assert response.json()["text"] == "Go"

    def test_a_two_letter_experience_bullet_is_still_refused(self, client: TestClient) -> None:
        # Only skills may be short; a two-character bullet is a parsing artefact.
        response = client.post(
            "/profile/items",
            json={"text": "ok", "kind": "experience_bullet"},
            headers=auth("idp|p2c"),
        )

        assert response.status_code == 422

    def test_contact_details_round_trip(self, client: TestClient) -> None:
        response = client.patch(
            "/profile",
            json={"full_name": "A Person", "contact_email": "a@example.test"},
            headers=auth("idp|p4"),
        )

        assert response.json()["full_name"] == "A Person"

    def test_an_item_can_be_removed(self, client: TestClient) -> None:
        created = client.post(
            "/profile/items", json={"text": ITEMS[0]}, headers=auth("idp|p5")
        ).json()

        assert (
            client.delete(f"/profile/items/{created['id']}", headers=auth("idp|p5")).status_code
            == 204
        )
        assert client.get("/profile", headers=auth("idp|p5")).json()["items"] == []

    def test_one_user_cannot_edit_anothers_item(self, client: TestClient) -> None:
        created = client.post(
            "/profile/items", json={"text": ITEMS[0]}, headers=auth("idp|owner3")
        ).json()

        response = client.patch(
            f"/profile/items/{created['id']}",
            json={"text": "changed"},
            headers=auth("idp|intruder3"),
        )

        assert response.status_code == 404


class TestFitScore:
    def test_a_requirement_finds_the_evidence_that_answers_it(self, client: TestClient) -> None:
        seed_profile(client, "idp|s1")
        job_id = ready_job(client, "idp|s1")

        score = client.get(f"/jobs/{job_id}/score", headers=auth("idp|s1")).json()

        matches = {match["requirement"]: match for match in score["matches"]}
        python = matches["Five years building backend services in Python"]
        assert python["coverage"] in {"covered", "partial"}
        assert "Python" in (python["evidence"] or "")

    def test_an_unanswered_requirement_is_reported_as_a_gap(self, client: TestClient) -> None:
        # Nothing in the profile mentions Kubernetes. The nearest item is always
        # *something*, and showing it beside a gap would read as a match the score
        # did not give.
        seed_profile(client, "idp|s2")
        job_id = ready_job(client, "idp|s2")

        score = client.get(f"/jobs/{job_id}/score", headers=auth("idp|s2")).json()

        matches = {match["requirement"]: match for match in score["matches"]}
        kubernetes = matches["Experience operating Kubernetes clusters in production"]
        assert kubernetes["coverage"] == "missing"
        assert kubernetes["evidence"] is None

    def test_the_score_is_reproducible(self, client: TestClient) -> None:
        # The property a model-produced score does not have.
        seed_profile(client, "idp|s3")
        job_id = ready_job(client, "idp|s3")

        first = client.get(f"/jobs/{job_id}/score", headers=auth("idp|s3")).json()
        second = client.get(f"/jobs/{job_id}/score", headers=auth("idp|s3")).json()

        assert first == second

    def test_every_requirement_appears_in_the_breakdown(self, client: TestClient) -> None:
        seed_profile(client, "idp|s4")
        job_id = ready_job(client, "idp|s4")

        detail = client.get(f"/jobs/{job_id}", headers=auth("idp|s4")).json()
        score = client.get(f"/jobs/{job_id}/score", headers=auth("idp|s4")).json()

        assert len(score["matches"]) == len(detail["requirements"])
        assert score["must_total"] == 3
        assert score["nice_total"] == 1

    def test_an_empty_profile_is_not_scorable_rather_than_zero(self, client: TestClient) -> None:
        # A zero means "nothing matched". Not scorable means "there was nothing to
        # match against", and those read very differently on a page.
        job_id = ready_job(client, "idp|s5")

        score = client.get(f"/jobs/{job_id}/score", headers=auth("idp|s5")).json()

        assert score["score"] == 0
        assert all(match["coverage"] == "missing" for match in score["matches"])

    def test_unreviewed_items_do_not_count(self, client: TestClient) -> None:
        # The fit score rests only on text a person has confirmed.
        seed_profile(client, "idp|s6")
        job_id = ready_job(client, "idp|s6")
        with_review = client.get(f"/jobs/{job_id}/score", headers=auth("idp|s6")).json()

        items = client.get("/profile", headers=auth("idp|s6")).json()["items"]
        for item in items:
            client.patch(
                f"/profile/items/{item['id']}", json={"reviewed": False}, headers=auth("idp|s6")
            )
        without = client.get(f"/jobs/{job_id}/score", headers=auth("idp|s6")).json()

        assert with_review["score"] > 0
        assert without["score"] == 0

    def test_the_embedding_space_is_reported(self, client: TestClient) -> None:
        # A score computed under the local backend is lexical, and the interface has
        # to be able to say so rather than presenting it as semantic.
        seed_profile(client, "idp|s7")
        job_id = ready_job(client, "idp|s7")

        score = client.get(f"/jobs/{job_id}/score", headers=auth("idp|s7")).json()

        assert score["embedding_model"] == "heuristic"

    def test_one_user_cannot_score_anothers_posting(self, client: TestClient) -> None:
        job_id = ready_job(client, "idp|s8")

        assert client.get(f"/jobs/{job_id}/score", headers=auth("idp|s9")).status_code == 404


class TestBoard:
    def test_starting_an_application_records_the_first_event(self, client: TestClient) -> None:
        # Without this row the history would begin at the first move, and the time an
        # application spent in Saved — where most of them die — would be unmeasurable.
        job_id = ready_job(client, "idp|b1")

        created = client.post("/applications", json={"job_id": job_id}, headers=auth("idp|b1"))

        assert created.status_code == 201
        body = created.json()
        assert body["stage"] == "saved"
        assert [event["to_stage"] for event in body["history"]] == ["saved"]
        assert body["history"][0]["from_stage"] is None

    def test_a_legal_move_appends_an_event(self, client: TestClient) -> None:
        job_id = ready_job(client, "idp|b2")
        application = client.post(
            "/applications", json={"job_id": job_id}, headers=auth("idp|b2")
        ).json()

        moved = client.post(
            f"/applications/{application['id']}/stage",
            json={"to_stage": "applied"},
            headers=auth("idp|b2"),
        )

        assert moved.status_code == 200
        assert moved.json()["stage"] == "applied"
        assert [event["to_stage"] for event in moved.json()["history"]] == ["saved", "applied"]

    def test_an_illegal_move_is_refused_by_the_domain_layer(self, client: TestClient) -> None:
        # Saved to Onsite skips three stages. The rule lives in one tested module and
        # this asserts the router surfaces its refusal rather than reimplementing it.
        job_id = ready_job(client, "idp|b3")
        application = client.post(
            "/applications", json={"job_id": job_id}, headers=auth("idp|b3")
        ).json()

        response = client.post(
            f"/applications/{application['id']}/stage",
            json={"to_stage": "onsite"},
            headers=auth("idp|b3"),
        )

        assert response.status_code == 409
        assert response.json()["code"] == "conflict"

    def test_any_stage_can_be_rejected(self, client: TestClient) -> None:
        job_id = ready_job(client, "idp|b4")
        application = client.post(
            "/applications", json={"job_id": job_id}, headers=auth("idp|b4")
        ).json()

        response = client.post(
            f"/applications/{application['id']}/stage",
            json={"to_stage": "rejected"},
            headers=auth("idp|b4"),
        )

        assert response.status_code == 200
        assert response.json()["is_active"] is False

    def test_the_interface_is_told_which_moves_are_legal(self, client: TestClient) -> None:
        # Derived from the same transition function the write path uses, so the two
        # cannot drift.
        job_id = ready_job(client, "idp|b5")
        application = client.post(
            "/applications", json={"job_id": job_id}, headers=auth("idp|b5")
        ).json()

        assert "applied" in application["allowed_next"]
        assert "onsite" not in application["allowed_next"]

    def test_the_board_groups_by_stage_and_separates_outcomes(self, client: TestClient) -> None:
        job_id = ready_job(client, "idp|b6")
        application = client.post(
            "/applications", json={"job_id": job_id}, headers=auth("idp|b6")
        ).json()
        client.post(
            f"/applications/{application['id']}/stage",
            json={"to_stage": "withdrawn"},
            headers=auth("idp|b6"),
        )

        board = client.get("/applications", headers=auth("idp|b6")).json()

        assert board["columns"][0]["stage"] == "saved"
        assert all(not column["applications"] for column in board["columns"])
        assert len(board["closed"]) == 1

    def test_tracking_the_same_posting_twice_is_refused(self, client: TestClient) -> None:
        job_id = ready_job(client, "idp|b7")
        client.post("/applications", json={"job_id": job_id}, headers=auth("idp|b7"))

        second = client.post("/applications", json={"job_id": job_id}, headers=auth("idp|b7"))

        assert second.status_code == 409

    def test_the_job_list_shows_which_postings_are_tracked(self, client: TestClient) -> None:
        job_id = ready_job(client, "idp|b8")
        client.post("/applications", json={"job_id": job_id}, headers=auth("idp|b8"))

        listed = client.get("/jobs", headers=auth("idp|b8")).json()

        assert listed[0]["application_id"] is not None

    def test_one_user_cannot_see_anothers_board(self, client: TestClient) -> None:
        job_id = ready_job(client, "idp|b9")
        client.post("/applications", json={"job_id": job_id}, headers=auth("idp|b9"))

        board = client.get("/applications", headers=auth("idp|b10")).json()

        assert board["closed"] == []
        assert all(not column["applications"] for column in board["columns"])
