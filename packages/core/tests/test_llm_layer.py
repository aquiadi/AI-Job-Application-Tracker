"""Prompt loading, cost estimation, cassette replay, and backend selection."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from jobtrack_core.config.settings import Settings, get_settings
from jobtrack_core.ingest.extraction import ExtractedPosting
from jobtrack_core.llm import LlmBackend, build_client
from jobtrack_core.llm.cassette import (
    CASSETTE_MODEL,
    Cassette,
    CassetteLlmClient,
    CassetteMissError,
    cassette_key,
)
from jobtrack_core.llm.client import InvalidOutputError, LlmError, LlmResult, Usage
from jobtrack_core.llm.heuristic import HEURISTIC_MODEL, HeuristicLlmClient
from jobtrack_core.llm.pricing import (
    PRICING_RETRIEVED,
    PRICING_SOURCE,
    RATES,
    estimate_cost_usd,
)
from jobtrack_core.llm.prompts import PromptError, RenderedPrompt, load_prompt


class TestPrompts:
    def test_the_extraction_prompt_exists_and_declares_its_slots(self) -> None:
        prompt = load_prompt("extract_jd")

        assert prompt.version == "v1"
        assert prompt.variables == {"posting", "title", "company"}

    def test_rendering_fills_every_slot(self) -> None:
        rendered = load_prompt("extract_jd").render(
            posting="- Five years of Python", title="Engineer", company="Northwind"
        )

        assert "{{" not in rendered.text
        assert "- Five years of Python" in rendered.text
        assert rendered.prompt_id == "extract_jd"

    def test_a_missing_value_is_refused(self) -> None:
        # A partial render would send `{{ posting }}` to the model as literal text.
        with pytest.raises(PromptError, match="posting"):
            load_prompt("extract_jd").render(title="Engineer", company="Northwind")

    def test_a_value_with_no_slot_is_refused(self) -> None:
        # Usually means the prompt was revised and the caller was not.
        with pytest.raises(PromptError, match="salary"):
            load_prompt("extract_jd").render(posting="x", title="y", company="z", salary="100000")

    def test_an_unknown_prompt_names_where_it_looked(self) -> None:
        with pytest.raises(PromptError, match="no prompt"):
            load_prompt("does_not_exist")

    def test_a_pinned_version_that_does_not_exist_is_an_error(self) -> None:
        # Silently falling back to the newest would make a pinned cassette replay
        # against a prompt it was not recorded for.
        with pytest.raises(PromptError, match="no v99"):
            load_prompt("extract_jd", version=99)

    def test_rendered_values_travel_with_the_text(self) -> None:
        # The heuristic backend reads these rather than parsing the rendered prompt.
        rendered = load_prompt("extract_jd").render(posting="body", title="t", company="c")

        assert rendered.values["posting"] == "body"


class TestPricing:
    def test_every_rate_carries_its_source(self) -> None:
        assert PRICING_SOURCE.startswith("https://")
        assert PRICING_RETRIEVED == "2026-09-11"

    def test_the_configured_models_are_priced(self) -> None:
        # A model id that is configurable but unpriced produces a cost report of zero,
        # which reads as free rather than as unknown.
        for model in ("gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-embedding-001"):
            assert model in RATES

    def test_it_prices_a_flash_lite_extraction(self) -> None:
        # 4,000 input and 1,000 output at 0.30 and 2.50 per million.
        cost = estimate_cost_usd("gemini-3.5-flash-lite", input_tokens=4_000, output_tokens=1_000)

        assert cost == Decimal("0.00370000")

    def test_cached_input_is_billed_at_its_own_rate(self) -> None:
        # Vertex reports cached tokens as a subset of the prompt count, not in
        # addition to it, so they must be subtracted before the full rate applies.
        cost = estimate_cost_usd(
            "gemini-3.5-flash", input_tokens=1_000, output_tokens=0, cached_input_tokens=1_000
        )

        assert cost == Decimal("0.00015000")

    def test_a_regional_endpoint_costs_ten_percent_more(self) -> None:
        base = estimate_cost_usd("gemini-3.5-flash", input_tokens=1_000_000, output_tokens=0)
        regional = estimate_cost_usd(
            "gemini-3.5-flash", input_tokens=1_000_000, output_tokens=0, regional=True
        )

        assert regional == (base * Decimal("1.10")).quantize(Decimal("0.00000001"))

    def test_local_backends_cost_nothing(self) -> None:
        assert estimate_cost_usd("heuristic", input_tokens=9_999, output_tokens=9_999) == 0

    def test_an_unknown_model_is_free_rather_than_fatal(self) -> None:
        # Refusing the call would lose the token counts too, and those cannot be
        # reconstructed later.
        assert estimate_cost_usd("gemini-9-ultra", input_tokens=100, output_tokens=100) == 0

    def test_the_estimate_keeps_eight_places(self) -> None:
        # One extraction costs a fraction of a cent; four places would round to zero.
        cost = estimate_cost_usd("gemini-3.5-flash-lite", input_tokens=10, output_tokens=0)

        assert cost > 0
        assert cost.as_tuple().exponent == -8


class TestCassettes:
    def test_the_key_names_the_prompt_and_its_version(self) -> None:
        prompt = RenderedPrompt(prompt_id="extract_jd", version="v1", text="body")

        assert cassette_key(prompt).startswith("extract_jd.v1.")

    def test_a_prompt_revision_does_not_reuse_old_recordings(self) -> None:
        first = cassette_key(RenderedPrompt(prompt_id="extract_jd", version="v1", text="body"))
        second = cassette_key(RenderedPrompt(prompt_id="extract_jd", version="v2", text="body"))

        assert first != second

    async def test_it_replays_a_recording(self, tmp_path: Path) -> None:
        prompt = RenderedPrompt(prompt_id="extract_jd", version="v1", text="a posting")
        key = cassette_key(prompt)
        (tmp_path / f"{key}.json").write_text(
            json.dumps(
                Cassette(
                    key=key,
                    model="gemini-3.5-flash-lite",
                    response_json=ExtractedPosting(title="Engineer").model_dump_json(),
                    usage=Usage(input_tokens=1200, output_tokens=300),
                    latency_ms=840,
                ).to_dict()
            )
        )

        result = await CassetteLlmClient(directory=tmp_path).generate_structured(
            prompt, ExtractedPosting
        )

        assert result.value.title == "Engineer"
        # The recorded model id, not the client's, so the row says what produced it.
        assert result.model == "gemini-3.5-flash-lite"
        assert result.usage.input_tokens == 1200

    async def test_a_miss_raises_and_names_the_file(self, tmp_path: Path) -> None:
        # Falling through to a live call would make the suite's credential
        # requirement depend on which cassettes happened to exist.
        prompt = RenderedPrompt(prompt_id="extract_jd", version="v1", text="unrecorded")

        with pytest.raises(CassetteMissError, match=r"no cassette extract_jd\.v1\."):
            await CassetteLlmClient(directory=tmp_path).generate_structured(
                prompt, ExtractedPosting
            )

    async def test_a_recording_that_no_longer_validates_is_a_finding(self, tmp_path: Path) -> None:
        # It means the schema changed under the fixture, which is worth failing on.
        prompt = RenderedPrompt(prompt_id="extract_jd", version="v1", text="a posting")
        key = cassette_key(prompt)
        (tmp_path / f"{key}.json").write_text(
            json.dumps(
                {
                    "key": key,
                    "model": "gemini-3.5-flash-lite",
                    "response_json": '{"experience_years_min": "not a number"}',
                    "usage": {},
                    "latency_ms": 0,
                }
            )
        )

        with pytest.raises(InvalidOutputError, match="no longer validates"):
            await CassetteLlmClient(directory=tmp_path).generate_structured(
                prompt, ExtractedPosting
            )

    async def test_recording_round_trips(self, tmp_path: Path) -> None:
        client = CassetteLlmClient(directory=tmp_path)
        prompt = RenderedPrompt(prompt_id="extract_jd", version="v1", text="a posting")
        recorded = LlmResult(
            value=ExtractedPosting(title="Engineer"),
            model="gemini-3.5-flash-lite",
            prompt_id="extract_jd",
            prompt_version="v1",
            usage=Usage(input_tokens=10, output_tokens=5),
            latency_ms=100,
            estimated_cost_usd=Decimal(0),
        )

        client.record(prompt, recorded)
        replayed = await client.generate_structured(prompt, ExtractedPosting)

        assert replayed.value == recorded.value

    async def test_embedding_does_not_need_a_recording(self, tmp_path: Path) -> None:
        # A cassette per embedded string would be thousands of files no test asserts on.
        result = await CassetteLlmClient(directory=tmp_path, embedding_dim=64).embed(["a", "b"])

        assert len(result.vectors) == 2
        assert result.model == CASSETTE_MODEL


class TestBackendSelection:
    def test_local_defaults_to_the_heuristic_backend(
        self, isolated_environment: pytest.MonkeyPatch
    ) -> None:
        # This is what makes the product runnable with no credentials at all.
        get_settings.cache_clear()
        client = build_client(Settings())

        assert isinstance(client, HeuristicLlmClient)
        assert client.generate_model == HEURISTIC_MODEL

    def test_cassette_is_selectable(self, isolated_environment: pytest.MonkeyPatch) -> None:
        isolated_environment.setenv("LLM_BACKEND", "cassette")
        get_settings.cache_clear()

        assert isinstance(build_client(Settings()), CassetteLlmClient)

    def test_cloud_refuses_a_local_backend(self, isolated_environment: pytest.MonkeyPatch) -> None:
        # A deployed process serving rule-based extraction while reporting itself
        # healthy is the failure this prevents.
        for key, value in {
            "ENVIRONMENT": "cloud",
            "GOOGLE_CLOUD_PROJECT": "p",
            "DB_ALLOYDB_INSTANCE_URI": "projects/p/locations/r/clusters/c/instances/i",
            "GCS_UPLOADS_BUCKET": "u",
            "GCS_RAW_BUCKET": "r",
            "GCS_ARTIFACTS_BUCKET": "a",
            "LLM_BACKEND": "heuristic",
        }.items():
            isolated_environment.setenv(key, value)
        get_settings.cache_clear()

        with pytest.raises(LlmError, match="must call Vertex"):
            build_client(Settings())

    def test_the_backend_names_are_the_configured_ones(self) -> None:
        assert {backend.value for backend in LlmBackend} == {"vertex", "heuristic", "cassette"}
