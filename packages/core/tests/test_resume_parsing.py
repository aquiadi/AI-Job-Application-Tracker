"""Resume parsing against a realistic document.

The fixture is a complete resume with the shapes that actually occur: a contact block,
two employers written with different separators, dates on their own line, a
comma-separated skills list, and an education section. Parsing it correctly is what the
fit score rests on — an item attributed to the wrong employer, or a skills line stored
as one row, degrades every score computed afterwards.
"""

from __future__ import annotations

from datetime import date

import pytest

from jobtrack_core.db.enums import ProfileItemKind
from jobtrack_core.llm.heuristic.resume import is_contact_line, parse
from jobtrack_core.profile.schemas import ParsedItem

RESUME = """ADITYA SHARMA
aditya.sharma@example.com | +1 415 555 0142 | San Francisco, CA
linkedin.com/in/adityasharma | github.com/adityasharma

Staff Software Engineer with nine years building payment and data infrastructure.

EXPERIENCE

Northwind Payments — Staff Software Engineer
Jan 2021 - Present
- Designed the idempotent ledger write path handling 40M postings per day
- Led the migration from a monolith to six Go services, cutting p99 latency by 43%
- Mentored four engineers, two of whom were promoted to senior

Helios Data, Senior Software Engineer
Mar 2018 - Dec 2020
- Built the streaming ingestion layer on Kafka processing 2TB daily
- Introduced Terraform modules adopted by every team in the company

PROJECTS
- pgvector-bench: an open source benchmark for approximate nearest neighbour recall

SKILLS
Python, Go, TypeScript, PostgreSQL, Kafka, Kubernetes, Terraform, AWS, gRPC

EDUCATION
B.Tech Computer Science, IIT Bombay, 2016
AWS Certified Solutions Architect, 2019
"""


@pytest.fixture(scope="module")
def parsed() -> list[ParsedItem]:
    return list(parse(RESUME).items)


def of(items: list[ParsedItem], kind: ProfileItemKind) -> list[ParsedItem]:
    return [item for item in items if item.kind is kind]


class TestContactDetails:
    def test_no_item_contains_the_email(self, parsed: list[ParsedItem]) -> None:
        # The strongest form of "contact details never reach a model" is that nothing
        # here can produce them. This is that rule, tested.
        assert not any("aditya.sharma@example.com" in item.text for item in parsed)

    def test_no_item_contains_the_phone_number(self, parsed: list[ParsedItem]) -> None:
        assert not any("555 0142" in item.text for item in parsed)

    def test_no_item_contains_a_profile_link(self, parsed: list[ParsedItem]) -> None:
        assert not any("linkedin.com" in item.text or "github.com" in item.text for item in parsed)

    def test_the_name_line_is_not_an_item(self, parsed: list[ParsedItem]) -> None:
        assert not any(item.text.strip() == "ADITYA SHARMA" for item in parsed)

    @pytest.mark.parametrize(
        "line",
        [
            "aditya.sharma@example.com | +1 415 555 0142",
            "linkedin.com/in/adityasharma",
            "https://example.dev",
            "ADITYA SHARMA",
            "Aditya Sharma",
        ],
    )
    def test_contact_shapes_are_recognised(self, line: str) -> None:
        assert is_contact_line(line)

    @pytest.mark.parametrize("line", ["EXPERIENCE", "SKILLS", "Education"])
    def test_section_headings_are_not_mistaken_for_a_name(self, line: str) -> None:
        # A heading is short and capitalised, which is also what a name looks like.
        assert not is_contact_line(line)


class TestEmployerAttribution:
    def test_each_bullet_carries_its_own_employer(self, parsed: list[ParsedItem]) -> None:
        # The bug this pins: an unrecognised second role line leaves the previous
        # employer attached, and every later bullet is credited to the wrong company.
        bullets = of(parsed, ProfileItemKind.EXPERIENCE_BULLET)
        ledger = next(item for item in bullets if "ledger" in item.text)
        kafka = next(item for item in bullets if "Kafka" in item.text)

        assert ledger.organisation == "Northwind Payments"
        assert kafka.organisation == "Helios Data"

    def test_both_separator_styles_are_read(self, parsed: list[ParsedItem]) -> None:
        # An em dash with spaces, and a comma with none.
        bullets = of(parsed, ProfileItemKind.EXPERIENCE_BULLET)
        organisations = {item.organisation for item in bullets}

        assert organisations == {"Northwind Payments", "Helios Data"}

    def test_the_title_is_told_apart_from_the_employer(self, parsed: list[ParsedItem]) -> None:
        # Decided by vocabulary rather than by which side of the separator it sits on,
        # because resumes write it both ways round.
        roles = {item.role for item in of(parsed, ProfileItemKind.EXPERIENCE_BULLET)}

        assert roles == {"Staff Software Engineer", "Senior Software Engineer"}


class TestDates:
    def test_a_date_line_below_the_role_is_attached_to_it(self, parsed: list[ParsedItem]) -> None:
        ledger = next(
            item for item in of(parsed, ProfileItemKind.EXPERIENCE_BULLET) if "ledger" in item.text
        )

        assert ledger.started_on == date(2021, 1, 1)

    def test_a_current_role_has_no_end_date(self, parsed: list[ParsedItem]) -> None:
        # Null `ended_on` means current everywhere else in the system.
        ledger = next(
            item for item in of(parsed, ProfileItemKind.EXPERIENCE_BULLET) if "ledger" in item.text
        )

        assert ledger.ended_on is None

    def test_a_closed_range_keeps_both_ends(self, parsed: list[ParsedItem]) -> None:
        kafka = next(
            item for item in of(parsed, ProfileItemKind.EXPERIENCE_BULLET) if "Kafka" in item.text
        )

        assert kafka.started_on == date(2018, 3, 1)
        assert kafka.ended_on == date(2020, 12, 1)


class TestSkills:
    def test_a_skills_line_becomes_one_item_per_skill(self, parsed: list[ParsedItem]) -> None:
        # Stored as one row it would be a single vector covering nine unrelated
        # technologies, matching every requirement weakly and none of them well.
        skills = {item.text for item in of(parsed, ProfileItemKind.SKILL)}

        assert skills == {
            "Python",
            "Go",
            "TypeScript",
            "PostgreSQL",
            "Kafka",
            "Kubernetes",
            "Terraform",
            "AWS",
            "gRPC",
        }

    def test_two_letter_skills_survive(self, parsed: list[ParsedItem]) -> None:
        # "Go", "R" and "C#" are exactly the tokens a posting screens on, and a blanket
        # three-character minimum drops them silently.
        assert any(item.text == "Go" for item in of(parsed, ProfileItemKind.SKILL))

    def test_a_one_character_skill_is_allowed(self) -> None:
        assert any(item.text == "R" for item in parse("SKILLS\nR, SAS, Stata").items)

    def test_a_short_experience_bullet_is_still_refused(self) -> None:
        # Only skills may be very short; a two-character bullet is a parsing artefact.
        assert not any(
            item.kind is ProfileItemKind.EXPERIENCE_BULLET
            for item in parse("EXPERIENCE\n- ok").items
        )


class TestSections:
    def test_projects_are_kept_apart_from_experience(self, parsed: list[ParsedItem]) -> None:
        projects = of(parsed, ProfileItemKind.PROJECT)

        assert len(projects) == 1
        assert "pgvector-bench" in projects[0].text

    def test_education_lines_become_items_without_needing_bullets(
        self, parsed: list[ParsedItem]
    ) -> None:
        education = {item.text for item in of(parsed, ProfileItemKind.EDUCATION)}

        assert "B.Tech Computer Science, IIT Bombay, 2016" in education
        assert "AWS Certified Solutions Architect, 2019" in education

    def test_the_opening_summary_becomes_the_headline(self) -> None:
        assert parse(RESUME).headline == (
            "Staff Software Engineer with nine years building payment and data infrastructure."
        )

    def test_an_empty_document_parses_to_nothing(self) -> None:
        result = parse("")

        assert result.items == []
        assert result.headline is None
