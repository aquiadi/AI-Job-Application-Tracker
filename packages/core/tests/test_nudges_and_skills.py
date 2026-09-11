"""Nudge timing, and the skills gap.

Both are small, deterministic and user-visible, which makes them worth pinning
precisely: a nudge that fires a day early is noise, and a skills gap that matches
"Docker" to "Kubernetes" is a lie told confidently.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from jobtrack_core.db.enums import ProfileItemKind
from jobtrack_core.domain.stages import Stage
from jobtrack_core.pipeline.nudges import (
    THRESHOLDS,
    business_days_between,
    is_stale,
)
from jobtrack_core.scoring.skills import _find, _pattern

FRIDAY = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class TestBusinessDays:
    def test_a_weekend_does_not_count(self) -> None:
        # A Friday application is not stale on Sunday, and counting calendar days
        # would nudge people about two days in which nobody was at work.
        friday = FRIDAY.date()
        monday = datetime(2026, 9, 7, 12, 0, tzinfo=UTC).date()

        assert business_days_between(friday, monday) == 1

    def test_a_full_week_is_five_days(self) -> None:
        start = datetime(2026, 9, 7, tzinfo=UTC).date()
        end = datetime(2026, 9, 14, tzinfo=UTC).date()

        assert business_days_between(start, end) == 5

    def test_the_same_day_is_zero(self) -> None:
        assert business_days_between(FRIDAY.date(), FRIDAY.date()) == 0

    def test_a_date_in_the_past_is_zero_rather_than_negative(self) -> None:
        later = datetime(2026, 9, 10, tzinfo=UTC).date()

        assert business_days_between(later, FRIDAY.date()) == 0


class TestStaleness:
    def test_an_application_under_its_threshold_is_not_stale(self) -> None:
        entered = datetime(2026, 9, 1, tzinfo=UTC)
        now = datetime(2026, 9, 3, tzinfo=UTC)

        assert not is_stale(Stage.APPLIED, entered, now=now)

    def test_an_application_past_its_threshold_is_stale(self) -> None:
        entered = datetime(2026, 8, 17, tzinfo=UTC)
        now = datetime(2026, 9, 4, tzinfo=UTC)

        assert is_stale(Stage.APPLIED, entered, now=now)

    def test_saved_never_nudges(self) -> None:
        # An application the user has not sent is not waiting on anyone. Nudging them
        # about their own inaction is the fastest way to make people stop opening it.
        entered = datetime(2026, 1, 1, tzinfo=UTC)

        assert not is_stale(Stage.SAVED, entered, now=FRIDAY)
        assert Stage.SAVED not in THRESHOLDS

    @pytest.mark.parametrize("stage", [Stage.REJECTED, Stage.WITHDRAWN, Stage.GHOSTED])
    def test_a_closed_application_never_nudges(self, stage: Stage) -> None:
        entered = datetime(2026, 1, 1, tzinfo=UTC)

        assert not is_stale(stage, entered, now=FRIDAY)

    def test_later_stages_are_chased_sooner(self) -> None:
        # A week of silence after an onsite means something different from a week
        # after submitting an application.
        assert THRESHOLDS[Stage.OFFER] < THRESHOLDS[Stage.ONSITE] < THRESHOLDS[Stage.APPLIED]


class TestSkillMatching:
    @pytest.mark.parametrize(
        ("skill", "text", "expected"),
        [
            ("Python", "Six years of Python at a payments company", True),
            ("Python", "Wrote Pythonic tooling", False),
            ("Go", "Migrated six services to Go", True),
            ("Go", "Django templates and Google Cloud", False),
            ("C++", "Ten years of C++ in trading systems", True),
            (".NET", "Maintained a .NET service", True),
            ("Node.js", "Built the Node.js gateway", True),
            ("R", "Statistical modelling in R", True),
            ("Kubernetes", "Deployed with Docker and Terraform", False),
        ],
    )
    def test_a_skill_matches_only_on_its_own(self, skill: str, text: str, expected: bool) -> None:
        # "Pythonic" is not Python and "Django" is not Go. A word-boundary `\\b` gets
        # `C++` and `.NET` wrong, which is why the pattern uses lookarounds over the
        # characters that legitimately appear inside a technology name.
        assert bool(_pattern(skill).search(text)) is expected

    def test_matching_is_case_insensitive(self) -> None:
        assert _pattern("postgresql").search("Tuned PostgreSQL indexes") is not None

    def test_the_shortest_evidence_is_preferred(self) -> None:
        # A one-word skill row is a cleaner citation than a forty-word bullet that
        # happens to contain the term.
        short = uuid.uuid4()
        long = uuid.uuid4()
        items = [
            (
                long,
                "Built the streaming ingestion layer on Kafka processing 2TB daily",
                ProfileItemKind.EXPERIENCE_BULLET,
            ),
            (short, "Kafka", ProfileItemKind.SKILL),
        ]

        found = _find("Kafka", items)

        assert found is not None
        assert found[0] == short

    def test_a_skill_with_no_evidence_is_not_matched(self) -> None:
        items = [(uuid.uuid4(), "Six years of Python", ProfileItemKind.SKILL)]

        assert _find("Kubernetes", items) is None
