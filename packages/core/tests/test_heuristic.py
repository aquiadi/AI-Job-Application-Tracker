"""The offline backend: segmentation and local embeddings.

These tests pin the floor. The heuristic backend is what the extraction eval measures
Gemini against, so a change that quietly makes it better or worse moves the baseline
and every comparison drawn against it.
"""

from __future__ import annotations

import pytest

from jobtrack_core.db.enums import RequirementKind
from jobtrack_core.ingest.extraction import ExtractedPosting
from jobtrack_core.llm.client import EmbeddingTaskType, LlmError
from jobtrack_core.llm.heuristic import HeuristicLlmClient, extract
from jobtrack_core.llm.heuristic.embed import embed_text
from jobtrack_core.llm.prompts import RenderedPrompt

POSTING = """
About the role

We are building the settlement platform.

What you'll do
- Own the ledger service end to end
- Partner with compliance on reconciliation

Requirements
- 5+ years building backend services in Python or Go
- Experience with distributed transactions
- Familiarity with Kubernetes would be great

Nice to have
- Event streaming with Kafka or Pub/Sub
- Authored reusable Terraform modules

Benefits
- Private medical cover
- 28 days holiday

The range for this role is $165,000 - $205,000 USD per year.
"""


def kinds(posting: ExtractedPosting) -> dict[str, RequirementKind]:
    return {requirement.text: requirement.kind for requirement in posting.requirements}


class TestSegmentation:
    def test_requirements_come_from_the_requirements_section(self) -> None:
        found = kinds(extract(POSTING))

        assert found["5+ years building backend services in Python or Go"] is RequirementKind.MUST
        assert found["Experience with distributed transactions"] is RequirementKind.MUST

    def test_a_nice_to_have_heading_marks_its_bullets(self) -> None:
        found = kinds(extract(POSTING))

        assert found["Event streaming with Kafka or Pub/Sub"] is RequirementKind.NICE
        assert found["Authored reusable Terraform modules"] is RequirementKind.NICE

    def test_a_bullet_can_mark_itself_optional_under_a_must_heading(self) -> None:
        # "Familiarity with" is hedged phrasing, and it sits under Requirements. The
        # bullet's own wording wins over the heading.
        assert kinds(extract(POSTING))["Familiarity with Kubernetes would be great"] is (
            RequirementKind.NICE
        )

    def test_responsibilities_are_not_requirements(self) -> None:
        posting = extract(POSTING)

        assert "Own the ledger service end to end" in posting.responsibilities
        assert "Own the ledger service end to end" not in kinds(posting)

    def test_benefits_are_dropped_entirely(self) -> None:
        # Perks under a Benefits heading are neither a requirement nor a
        # responsibility, and scoring a candidate against "28 days holiday" is noise.
        #
        # The bullet here carries must-phrasing on purpose. Without the Benefits
        # heading being recognised it would be classified as a requirement, so this
        # fails if the heading pattern stops matching rather than passing by default.
        posting = extract("Benefits\n- You must have 25 days of holiday\n- Private medical cover")

        assert posting.requirements == []
        assert posting.responsibilities == []

    def test_perks_and_benefits_headings_both_count_as_noise(self) -> None:
        for heading in ("Benefits", "Perks", "Compensation", "Equal Opportunity"):
            posting = extract(f"{heading}\n- You must be eligible for our stock plan")

            assert posting.requirements == [], heading

    def test_it_reads_the_smallest_stated_experience_requirement(self) -> None:
        assert extract(POSTING).experience_years_min == 5

    def test_it_reads_a_salary_range(self) -> None:
        posting = extract(POSTING)

        assert posting.salary_min == 165000
        assert posting.salary_max == 205000
        assert posting.salary_currency == "USD"

    def test_it_names_only_technologies_that_appear(self) -> None:
        skills = extract(POSTING).hard_skills

        assert {"python", "go", "kubernetes", "kafka", "terraform"} <= set(skills)
        assert "rust" not in skills

    def test_an_unheaded_bullet_is_classified_on_phrasing(self) -> None:
        posting = extract("- Must have production Rust experience\n- Kafka is a plus")

        assert kinds(posting) == {
            "Must have production Rust experience": RequirementKind.MUST,
            "Kafka is a plus": RequirementKind.NICE,
        }

    def test_an_unheaded_bullet_with_no_signal_is_left_out(self) -> None:
        # Guessing here produces requirements the posting never made, which is the
        # failure the eval's hallucinated-field rate measures.
        assert extract("- We use a monorepo").requirements == []

    def test_it_invents_no_fields(self) -> None:
        posting = extract("Requirements\n- Five years of Python")

        assert posting.seniority is None
        assert posting.remote_policy is None
        assert posting.salary_min is None
        assert posting.employment_type is None


class TestLocalEmbeddings:
    def test_it_is_deterministic_across_processes(self) -> None:
        # blake2b rather than the built-in hash, which PYTHONHASHSEED randomises per
        # process. A vector stored today has to compare against one computed tomorrow.
        assert embed_text("Python backend services", 768) == embed_text(
            "Python backend services", 768
        )

    def test_vectors_are_unit_length(self) -> None:
        vector = embed_text("Kubernetes in production", 768)

        assert sum(value * value for value in vector) == pytest.approx(1.0)

    def test_shared_vocabulary_scores_above_unrelated_text(self) -> None:
        # This is a lexical space, so this is the whole of what it can do. It is also
        # why M3's threshold calibration has to run against real embeddings.
        requirement = embed_text("5+ years building backend services in Python", 768)
        related = embed_text("Six years building Python backend services", 768)
        unrelated = embed_text("Managed a portfolio of commercial real estate", 768)

        assert _cosine(requirement, related) > _cosine(requirement, unrelated)

    def test_empty_text_gives_a_usable_vector(self) -> None:
        # A zero vector would make every cosine comparison undefined.
        vector = embed_text("", 768)

        assert sum(value * value for value in vector) == pytest.approx(1.0)

    def test_the_width_is_what_was_asked_for(self) -> None:
        assert len(embed_text("anything", 256)) == 256


class TestHeuristicClient:
    async def test_it_answers_the_extraction_prompt(self) -> None:
        client = HeuristicLlmClient()
        prompt = RenderedPrompt(
            prompt_id="extract_jd",
            version="v1",
            text="ignored by this backend",
            values={"posting": POSTING, "title": "Backend Engineer", "company": "Northwind"},
        )

        result = await client.generate_structured(prompt, ExtractedPosting)

        assert result.value.title == "Backend Engineer"
        assert result.model == "heuristic"
        # Token counts stay zero rather than being estimated: a fabricated count would
        # flow into the cost report and price a call that cost nothing.
        assert result.usage.input_tokens == 0
        assert result.estimated_cost_usd == 0

    async def test_an_unimplemented_prompt_raises_rather_than_returning_nothing(self) -> None:
        # A backend that silently produced an empty object would be indistinguishable
        # from a model that found nothing in the posting.
        client = HeuristicLlmClient()
        prompt = RenderedPrompt(prompt_id="tailor_resume", version="v1", text="", values={})

        with pytest.raises(LlmError, match="no implementation"):
            await client.generate_structured(prompt, ExtractedPosting)

    async def test_embedding_preserves_order(self) -> None:
        client = HeuristicLlmClient(embedding_dim=128)

        result = await client.embed(["first", "second", "third"])

        assert result.vectors[0] == embed_text("first", 128)
        assert result.vectors[2] == embed_text("third", 128)
        assert result.dim == 128
        assert result.task_type is EmbeddingTaskType.SEMANTIC_SIMILARITY


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))
