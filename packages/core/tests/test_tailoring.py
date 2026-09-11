"""The rule-based tailor.

It selects and orders rather than rewriting, so what these check is the document's
shape: everything is one of the user's own lines, relevance decides order, and the
content that is not a bullet does not get forced into being one.
"""

from __future__ import annotations

import uuid

from jobtrack_core.db.enums import ProfileItemKind
from jobtrack_core.generate.schemas import MIN_BULLET_CHARS
from jobtrack_core.generate.validator import Evidence, validate
from jobtrack_core.llm.heuristic.tailor import SKILLS_HEADING, tailor

LEDGER = uuid.uuid4()
KAFKA = uuid.uuid4()
GO = uuid.uuid4()
AWS = uuid.uuid4()
POSTGRES = uuid.uuid4()

ITEMS = [
    (
        LEDGER,
        "Designed the idempotent ledger write path",
        "Northwind",
        "Staff Engineer",
        ProfileItemKind.EXPERIENCE_BULLET,
    ),
    (
        KAFKA,
        "Built the streaming ingestion layer on Kafka",
        "Helios",
        "Senior Engineer",
        ProfileItemKind.EXPERIENCE_BULLET,
    ),
    (GO, "Go", None, None, ProfileItemKind.SKILL),
    (AWS, "AWS", None, None, ProfileItemKind.SKILL),
    (POSTGRES, "PostgreSQL", None, None, ProfileItemKind.SKILL),
]


class TestShape:
    def test_every_bullet_is_one_of_the_users_own_lines(self) -> None:
        resume = tailor(items=ITEMS, relevant_item_ids=[LEDGER])

        texts = {bullet.text for bullet in resume.bullets()}
        assert "Designed the idempotent ledger write path" in texts

    def test_every_bullet_cites_its_source(self) -> None:
        resume = tailor(items=ITEMS, relevant_item_ids=[LEDGER])

        assert all(bullet.source_item_ids for bullet in resume.bullets())

    def test_relevant_items_lead(self) -> None:
        # Ordering is the tailoring here. What a reader sees first is the decision.
        resume = tailor(items=ITEMS, relevant_item_ids=[KAFKA])

        assert resume.sections[0].organisation == "Helios"

    def test_items_the_breakdown_missed_are_still_included(self) -> None:
        # An earlier version kept only matched items, which on a posting that matched
        # one requirement produced a one-bullet resume: relevant and useless to send.
        resume = tailor(items=ITEMS, relevant_item_ids=[KAFKA])

        organisations = {section.organisation for section in resume.sections}
        assert {"Northwind", "Helios"} <= organisations

    def test_bullets_are_grouped_by_employer(self) -> None:
        resume = tailor(items=ITEMS, relevant_item_ids=[])

        headings = [section.heading for section in resume.sections]
        assert "Northwind" in headings
        assert "Helios" in headings

    def test_it_writes_no_summary(self) -> None:
        # Composing one would mean inventing the connective tissue between items,
        # which is exactly what a rule cannot do honestly.
        assert tailor(items=ITEMS, relevant_item_ids=[]).summary is None


class TestSkills:
    def test_short_skills_become_one_line_rather_than_bullets(self) -> None:
        # The bug this pins: "Go" and "AWS" are shorter than a bullet is allowed to
        # be, and emitting them as bullets failed schema validation inside generation
        # — which surfaced as a 500 on every profile built from an imported resume.
        resume = tailor(items=ITEMS, relevant_item_ids=[])

        skills = next(section for section in resume.sections if section.heading == SKILLS_HEADING)
        assert skills.bullets[0].text == "Go, AWS, PostgreSQL"

    def test_the_skills_line_cites_every_skill_it_collected(self) -> None:
        resume = tailor(items=ITEMS, relevant_item_ids=[])

        skills = next(section for section in resume.sections if section.heading == SKILLS_HEADING)
        assert set(skills.bullets[0].source_item_ids) == {GO, AWS, POSTGRES}

    def test_a_profile_with_no_skills_gets_no_skills_section(self) -> None:
        resume = tailor(items=ITEMS[:2], relevant_item_ids=[])

        assert all(section.heading != SKILLS_HEADING for section in resume.sections)

    def test_a_trivially_short_skills_line_is_dropped_rather_than_padded(self) -> None:
        # "Go" alone is under the bullet minimum even once joined. Dropping it is the
        # honest outcome: a one-word Skills section on a resume says nothing, and
        # padding it to clear a length check would be inventing text.
        resume = tailor(items=[ITEMS[0], ITEMS[2]], relevant_item_ids=[])

        assert all(section.heading != SKILLS_HEADING for section in resume.sections)

    def test_a_prose_item_under_the_minimum_is_skipped(self) -> None:
        short = uuid.uuid4()
        items = [*ITEMS, (short, "Did it", None, None, ProfileItemKind.EXPERIENCE_BULLET)]

        resume = tailor(items=items, relevant_item_ids=[])

        assert all(len(bullet.text) >= MIN_BULLET_CHARS for bullet in resume.bullets())


class TestValidation:
    def test_the_whole_document_passes_the_grounding_validator(self) -> None:
        # True by construction here, and that is the point: the enforcement path is
        # exercised on a real document offline rather than mocked.
        resume = tailor(items=ITEMS, relevant_item_ids=[LEDGER, KAFKA])
        evidence = Evidence(texts={item[0]: item[1] for item in ITEMS})

        result = validate(resume.bullets(), evidence)

        assert result.rejected == []
        assert len(result.accepted) == len(resume.bullets())
