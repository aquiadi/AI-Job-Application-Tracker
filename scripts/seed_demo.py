"""Create a demo account with real data, so the product can be judged by clicking it.

Everything here goes through the public HTTP API with a real signed-in token. Nothing
reaches into the database, which means a successful run is evidence the API works —
if this script finishes, every endpoint it touched works for a browser too.

The account is created in the Firebase Auth emulator, so it exists only on this
machine and disappears when the emulator stops.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# Every URL below is a fixed localhost address built from these constants; none of
# them is influenced by input, which is why the S310 suppressions are safe.
API = "http://127.0.0.1:8080"
EMULATOR = "http://127.0.0.1:9099"
WEB = "http://127.0.0.1:3000"

EMAIL = "demo@example.test"
PASSWORD = "demo-password"  # noqa: S105 - a local emulator fixture, not a secret

RESUME_ITEMS = [
    (
        "Designed the idempotent ledger write path handling 40,000,000 postings per day",
        "experience_bullet",
        "Northwind Payments",
        "Staff Software Engineer",
    ),
    (
        "Led the migration from a monolith to six Go services, cutting p99 latency by 43%",
        "experience_bullet",
        "Northwind Payments",
        "Staff Software Engineer",
    ),
    (
        "Tuned PostgreSQL queries and indexes for a 40,000,000 row ledger",
        "experience_bullet",
        "Northwind Payments",
        "Staff Software Engineer",
    ),
    (
        "Mentored four engineers, two of whom were promoted to senior",
        "experience_bullet",
        "Northwind Payments",
        "Staff Software Engineer",
    ),
    (
        "Built the streaming ingestion layer on Kafka processing 2TB daily",
        "experience_bullet",
        "Helios Data",
        "Senior Software Engineer",
    ),
    (
        "Introduced Terraform modules adopted by every team in the company",
        "experience_bullet",
        "Helios Data",
        "Senior Software Engineer",
    ),
    (
        "Ran the on-call rotation for the data platform",
        "experience_bullet",
        "Helios Data",
        "Senior Software Engineer",
    ),
    ("Python", "skill", None, None),
    ("Go", "skill", None, None),
    ("PostgreSQL", "skill", None, None),
    ("Kafka", "skill", None, None),
    ("Terraform", "skill", None, None),
    ("B.Tech Computer Science, IIT Bombay", "education", None, None),
]


@dataclass(frozen=True, slots=True)
class Posting:
    """One seeded posting, and where its application should end up."""

    title: str
    company: str
    #: None means saved but not tracked, so the board has an untracked posting too.
    stage: str | None
    days_ago: int
    text: str


POSTINGS = [
    Posting(
        title="Senior Backend Engineer, Payments",
        company="Aurora Financial",
        stage="applied",
        # Old enough that the nudge sweep has something to find.
        days_ago=18,
        text="""About the role

Aurora runs the settlement rails for marketplace payouts.

What you'll do
- Own the ledger service end to end
- Partner with compliance on reconciliation

Requirements
- 5+ years building backend services in Python or Go
- Experience with distributed transactions and idempotent processing
- Deep knowledge of PostgreSQL performance
- Operating services on Kubernetes in production

Nice to have
- Event streaming with Kafka or Pub/Sub
- Authored reusable Terraform modules

Benefits
- Private medical cover
- 28 days holiday""",
    ),
    Posting(
        title="Staff Data Platform Engineer",
        company="Mercator",
        stage="screen",
        days_ago=6,
        text="""Mercator moves supply chain data for freight forwarders.

What you will do
- Design the ingestion layer for carrier feeds
- Own data quality SLAs

Required Qualifications
- Minimum 6 years building data pipelines
- Deep SQL and Python
- Production experience with Airflow or dbt
- Experience running streaming systems such as Kafka

Nice to have
- Exposure to Snowflake or BigQuery""",
    ),
    Posting(
        title="Principal Engineer, Infrastructure",
        company="Ferrous",
        # Saved but never tracked, so the board shows the untracked case too.
        stage=None,
        days_ago=0,
        text="""Ferrous runs payments infrastructure for marketplaces.

Requirements
- Ten years of engineering experience, several at staff level or above
- Deep expertise in Rust or C++
- Experience designing multi-region systems
- Track record of mentoring senior engineers

Nice to have
- Public speaking or writing about systems work""",
    ),
]


def post(url: str, payload: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, method="POST")  # noqa: S310
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed localhost URLs
        raw = response.read()
    return json.loads(raw) if raw else {}


def patch(url: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, method="PATCH")  # noqa: S310
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        raw = response.read()
    return json.loads(raw) if raw else {}


def get(url: str, token: str) -> Any:
    request = urllib.request.Request(url)  # noqa: S310
    request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return json.loads(response.read())


def sign_in() -> str:
    """Create the demo account in the emulator, or sign in if it already exists."""
    for action in ("signUp", "signInWithPassword"):
        url = f"{EMULATOR}/identitytoolkit.googleapis.com/v1/accounts:{action}?key=demo-api-key"
        try:
            answer = post(url, {"email": EMAIL, "password": PASSWORD, "returnSecureToken": True})
            return str(answer["idToken"])
        except urllib.error.HTTPError as error:
            if action == "signInWithPassword":
                detail = error.read().decode()
                raise SystemExit(f"Could not sign in to the emulator: {detail}") from error
    raise SystemExit("unreachable")


def wait_ready(token: str, job_id: str) -> str:
    """Poll until extraction and embedding finish, the way the browser does."""
    import time

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = get(f"{API}/jobs/{job_id}", token)
        if job["status"] in {"ready", "failed"}:
            return str(job["status"])
        time.sleep(0.3)
    return "timeout"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        return seed()
    except urllib.error.HTTPError as error:
        print(f"{error.code} from {error.url}\n{error.read().decode()[:800]}", file=sys.stderr)
        return 1


def seed() -> int:

    try:
        token = sign_in()
    except urllib.error.URLError:
        print(f"The auth emulator is not answering at {EMULATOR}. Run `make up`.", file=sys.stderr)
        return 1

    try:
        get(f"{API}/me", token)
    except urllib.error.URLError:
        print(f"The api is not answering at {API}. Run `make dev`.", file=sys.stderr)
        return 1

    print("==> profile")
    patch(
        f"{API}/profile",
        {
            "full_name": "Demo Candidate",
            "contact_email": "demo@example.test",
            "phone": "+1 415 555 0142",
            "location": "San Francisco, CA",
            "headline": "Staff engineer, payments and data infrastructure",
        },
        token,
    )
    existing = {item["text"] for item in get(f"{API}/profile", token)["items"]}
    for text, kind, organisation, role in RESUME_ITEMS:
        if text in existing:
            continue
        post(
            f"{API}/profile/items",
            {"text": text, "kind": kind, "organisation": organisation, "role": role},
            token,
        )
    print(f"    {len(RESUME_ITEMS)} items")

    print("==> postings")
    for posting in POSTINGS:
        saved = post(
            f"{API}/jobs",
            {"text": posting.text, "title": posting.title, "company": posting.company},
            token,
        )
        job_id = saved["id"]
        status = wait_ready(token, job_id)
        score = get(f"{API}/jobs/{job_id}/score", token)
        shown = len(score["skills"]["have"])
        asked = shown + len(score["skills"]["lack"])
        print(
            f"    {posting.title:36} {status:8} fit {score['score']:>3}/100  {shown}/{asked} skills"
        )

        if posting.stage is None:
            continue

        application = post(f"{API}/applications", {"job_id": job_id}, token)
        entered = datetime.now(UTC) - timedelta(days=posting.days_ago)
        # Saved -> Applied -> ... one legal step at a time, because the domain layer
        # refuses anything else and this script is not allowed a shortcut the
        # interface does not have.
        for stage in ("applied", "screen")[: 1 if posting.stage == "applied" else 2]:
            post(
                f"{API}/applications/{application['id']}/stage",
                {"to_stage": stage, "occurred_at": entered.isoformat()},
                token,
            )
        post(f"{API}/applications/{application['id']}/tailor", {}, token)

    print("==> follow-ups")
    sweep = post(f"{API}/nudges/sweep", {}, token)
    print(f"    {sweep['drafted']} drafted from {sweep['considered']} stalled")

    print(
        f"\nOpen {WEB}\n"
        f"  email     {EMAIL}\n"
        f"  password  {PASSWORD}\n\n"
        "The account lives in the local auth emulator and disappears with it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
