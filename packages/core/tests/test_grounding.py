"""The grounding validator.

This is the check that decides whether a generated resume can be trusted, so these
tests are written as the attacks they defend against: a bullet that cites nothing, a
bullet that cites an id it invented, a bullet that adds a percentage, and a bullet that
adds a technology. ADR 13.
"""

from __future__ import annotations

import uuid

import pytest

from jobtrack_core.generate.schemas import TailoredBullet
from jobtrack_core.generate.validator import Evidence, validate, validate_bullet

LEDGER = uuid.uuid4()
KAFKA = uuid.uuid4()

EVIDENCE = Evidence(
    texts={
        LEDGER: "Designed the idempotent ledger write path handling 40,000,000 postings per day",
        KAFKA: "Built the streaming ingestion layer on Kafka processing 2TB daily",
    }
)


def bullet(text: str, *sources: uuid.UUID) -> TailoredBullet:
    return TailoredBullet(text=text, source_item_ids=list(sources))


class TestCitations:
    def test_a_bullet_citing_nothing_is_refused(self) -> None:
        # Pydantic refuses it at the schema, before the validator is reached: the
        # field has a minimum length of one, so an uncited bullet cannot be built.
        with pytest.raises(ValueError, match="source_item_ids"):
            bullet("Led the migration to microservices")

    def test_an_invented_id_is_refused(self) -> None:
        # A model that cannot find a source sometimes produces a plausible-looking id.
        reason = validate_bullet(bullet("Did something impressive", uuid.uuid4()), EVIDENCE)

        assert reason is not None
        assert "not available" in reason

    def test_an_id_belonging_to_someone_else_is_refused(self) -> None:
        # Evidence is built from the caller's reviewed items only, so another user's
        # id is simply absent — the same path as an invented one, which is correct.
        assert validate_bullet(bullet("Built a thing", uuid.uuid4()), EVIDENCE) is not None

    def test_a_grounded_bullet_passes(self) -> None:
        assert (
            validate_bullet(bullet("Designed the idempotent ledger write path", LEDGER), EVIDENCE)
            is None
        )


class TestInventedNumbers:
    def test_an_added_percentage_is_refused(self) -> None:
        # The single most dangerous output: a resume is believed on its numbers, and
        # this is the one a reader will ask about.
        reason = validate_bullet(
            bullet("Designed the ledger write path, cutting latency by 43%", LEDGER), EVIDENCE
        )

        assert reason is not None
        assert "43" in reason

    def test_a_number_from_the_evidence_is_kept(self) -> None:
        assert (
            validate_bullet(
                bullet("Handled 40,000,000 postings per day on the ledger", LEDGER), EVIDENCE
            )
            is None
        )

    def test_reformatting_a_number_is_allowed(self) -> None:
        # "40M" from "40,000,000" is the same claim written for a resume, and
        # rejecting it would make the validator fight the thing it is meant to permit.
        assert validate_bullet(bullet("Handled 40M postings per day", LEDGER), EVIDENCE) is None

    def test_small_counts_are_not_treated_as_claims(self) -> None:
        # "two services" is ordinary English, not a metric.
        assert validate_bullet(bullet("Split the ledger into 2 services", LEDGER), EVIDENCE) is None

    def test_a_computed_score_may_be_quoted(self) -> None:
        # Figures from the fit breakdown came from this system, not from the model.
        assert (
            validate_bullet(
                bullet("Matched 87 percent of the stated requirements", LEDGER),
                EVIDENCE,
                allowed_numbers=frozenset({"87"}),
            )
            is None
        )


class TestInventedSkills:
    def test_a_technology_the_evidence_never_mentions_is_refused(self) -> None:
        # The candidate may well know Kubernetes. This bullet does not show it, and a
        # resume line is a claim the reader will test.
        reason = validate_bullet(bullet("Ran the ledger service on Kubernetes", LEDGER), EVIDENCE)

        assert reason is not None
        assert "kubernetes" in reason.lower()

    def test_a_technology_from_the_cited_item_is_kept(self) -> None:
        assert validate_bullet(bullet("Built the Kafka ingestion layer", KAFKA), EVIDENCE) is None

    def test_evidence_is_pooled_across_every_cited_item(self) -> None:
        # A bullet may legitimately draw on two items, and each contributes its terms.
        assert (
            validate_bullet(bullet("Moved ledger postings through Kafka", LEDGER, KAFKA), EVIDENCE)
            is None
        )

    def test_rephrasing_without_naming_a_technology_is_allowed(self) -> None:
        # Only known technologies are checked. An open-ended noun check would reject
        # every honest rewrite, which is the thing tailoring is for.
        assert (
            validate_bullet(
                bullet("Made the write path safe to retry without double-counting", LEDGER),
                EVIDENCE,
            )
            is None
        )


class TestBatch:
    def test_good_bullets_survive_a_bad_one(self) -> None:
        # One rejection must not discard the document. A shorter resume with a visible
        # gap is the intended outcome.
        result = validate(
            [
                bullet("Designed the idempotent ledger write path", LEDGER),
                bullet("Cut latency by 43%", LEDGER),
                bullet("Built the Kafka ingestion layer", KAFKA),
            ],
            EVIDENCE,
        )

        assert len(result.accepted) == 2
        assert len(result.rejected) == 1

    def test_every_rejection_carries_a_readable_reason(self) -> None:
        # The reason is stored on the artifact and shown, so it has to make sense to
        # the person reading it rather than name an internal rule.
        result = validate([bullet("Cut latency by 43%", LEDGER)], EVIDENCE)

        assert result.warnings
        assert "43" in result.warnings[0]
