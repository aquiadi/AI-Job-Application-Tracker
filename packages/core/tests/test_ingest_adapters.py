"""Adapters against recorded payloads.

The fixtures reproduce the envelope of each board's API — field names, nesting, and
Greenhouse's HTML-escaped `content` — as verified against the live endpoints on
2026-09-11. The posting prose inside them is invented, because the adapter parses the
envelope and the prose is only there to be carried through.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobtrack_core.db.enums import SourceAts
from jobtrack_core.ingest.adapters.base import PostingParseError, UnsupportedSourceError
from jobtrack_core.ingest.adapters.greenhouse import GreenhouseAdapter
from jobtrack_core.ingest.adapters.lever import LeverAdapter
from jobtrack_core.ingest.adapters.pasted import MIN_BODY_CHARS, PastedAdapter
from jobtrack_core.ingest.router import adapter_for, parse_pasted

FIXTURES = Path(__file__).parent / "fixtures"

GREENHOUSE_URL = "https://job-boards.greenhouse.io/northwind/jobs/6136160004"
LEVER_URL = "https://jobs.lever.co/mercator/7fca4a70-174c-41a2-b44b-7ff1cb9422e7"


def payload(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class TestGreenhouse:
    def test_it_recognises_both_host_spellings(self) -> None:
        # Greenhouse's own absolute_url returns job-boards; users paste whichever
        # their browser shows, and both are live.
        adapter = GreenhouseAdapter()

        assert adapter.matches("https://boards.greenhouse.io/northwind/jobs/6136160004")
        assert adapter.matches(GREENHOUSE_URL)
        assert not adapter.matches("https://northwind.com/careers/6136160004")

    def test_the_fetched_url_is_the_api_not_the_page(self) -> None:
        # The page is a JavaScript shell. The board API is the documented contract,
        # and building it from a template is also what keeps this free of SSRF.
        assert GreenhouseAdapter().api_url(GREENHOUSE_URL) == (
            "https://boards-api.greenhouse.io/v1/boards/northwind/jobs/6136160004?questions=false"
        )

    def test_it_normalises_a_posting(self) -> None:
        posting = GreenhouseAdapter().parse(
            payload("greenhouse_job.json"), source_url=GREENHOUSE_URL
        )

        assert posting.source_ats is SourceAts.GREENHOUSE
        assert posting.title == "Senior Backend Engineer, Payments"
        assert posting.company == "Northwind"
        assert posting.location == "Hybrid - London"
        # first_published is preferred over updated_at: a posting edited today is
        # not a posting listed today, and the board sorts on the former.
        assert posting.posted_at is not None
        assert posting.posted_at.astimezone(UTC) == datetime(2026, 8, 6, 16, 50, 10, tzinfo=UTC)

    def test_escaped_markup_becomes_bullets(self) -> None:
        # `content` arrives double-encoded: the markup is an HTML-escaped string, so a
        # parser that does not unescape first reads `&lt;li&gt;` as literal text and
        # the entire list collapses into one line.
        posting = GreenhouseAdapter().parse(
            payload("greenhouse_job.json"), source_url=GREENHOUSE_URL
        )

        assert "- 5+ years building backend services in Python or Go" in posting.body
        assert "&lt;" not in posting.body
        assert "<li>" not in posting.body

    def test_a_payload_without_content_is_an_error(self) -> None:
        with pytest.raises(PostingParseError, match="no posting content"):
            GreenhouseAdapter().parse(b'{"title": "Gone"}', source_url=GREENHOUSE_URL)

    def test_html_that_is_not_json_is_an_error(self) -> None:
        with pytest.raises(PostingParseError, match="not JSON"):
            GreenhouseAdapter().parse(b"<html>404</html>", source_url=GREENHOUSE_URL)


class TestLever:
    def test_it_builds_the_postings_api_url(self) -> None:
        assert LeverAdapter().api_url(LEVER_URL) == (
            "https://api.lever.co/v0/postings/mercator/"
            "7fca4a70-174c-41a2-b44b-7ff1cb9422e7?mode=json"
        )

    def test_section_headings_survive(self) -> None:
        # "Required Qualifications" above a block of bullets is what tells extraction
        # those bullets are must-haves. Dropping the heading loses a signal no prompt
        # wording recovers.
        posting = LeverAdapter().parse(payload("lever_job.json"), source_url=LEVER_URL)

        assert "Required Qualifications" in posting.body
        assert "Nice to have" in posting.body
        assert "- Minimum 6 years building data pipelines" in posting.body

    def test_it_reads_metadata_lever_keeps_outside_the_description(self) -> None:
        posting = LeverAdapter().parse(payload("lever_job.json"), source_url=LEVER_URL)

        assert posting.title == "Staff Data Engineer"
        assert posting.location == "Seattle, WA"
        # Lever has no company field; the board slug in the hosted URL is the company.
        assert posting.company == "Mercator"
        # workplaceType appears nowhere in the prose, so it is appended to the body
        # for extraction to read rather than being set on a column here.
        assert "Workplace type: hybrid" in posting.body

    def test_createdat_is_epoch_milliseconds(self) -> None:
        posting = LeverAdapter().parse(payload("lever_job.json"), source_url=LEVER_URL)

        assert posting.posted_at == datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

    def test_an_error_document_is_reported(self) -> None:
        # Lever answers 200 with this body for a deleted posting.
        with pytest.raises(PostingParseError, match="Document not found"):
            LeverAdapter().parse(
                json.dumps({"ok": False, "error": "Document not found"}).encode(),
                source_url=LEVER_URL,
            )


class TestPasted:
    def test_it_accepts_plain_text(self) -> None:
        text = "Requirements\n- 5 years of Python\n- Experience with Postgres\n" + "x" * 120
        posting = parse_pasted(text)

        assert posting.source_ats is SourceAts.PASTED
        assert "- 5 years of Python" in posting.body

    def test_it_converts_markup_when_someone_pastes_source(self) -> None:
        html = (
            "<ul><li>Five years of Python</li><li>Postgres in production</li></ul>"
            f"<p>{'x' * 140}</p>"
        )
        posting = parse_pasted(html)

        assert "- Five years of Python" in posting.body
        assert "<li>" not in posting.body

    def test_site_chrome_is_removed(self) -> None:
        text = "\n".join(["Apply now", "Share this job", "- Five years of Python", "x" * 140])
        posting = parse_pasted(text)

        assert "Apply now" not in posting.body
        assert "Share this job" not in posting.body
        assert "- Five years of Python" in posting.body

    def test_a_requirement_containing_apply_survives(self) -> None:
        # The chrome patterns match whole lines. A requirement that merely contains
        # "apply" is not chrome, and stripping it would silently drop a requirement.
        text = "- Apply statistical methods to pricing models\n" + "x" * 140
        posting = parse_pasted(text)

        assert "Apply statistical methods to pricing models" in posting.body

    def test_too_short_is_refused_with_a_usable_message(self) -> None:
        with pytest.raises(PostingParseError, match=str(MIN_BODY_CHARS)):
            parse_pasted("Senior Engineer")

    def test_it_never_matches_a_url(self) -> None:
        # Pasted is chosen explicitly by the caller. If it matched URLs it would
        # shadow every real adapter.
        assert not PastedAdapter().matches(GREENHOUSE_URL)


class TestRouting:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (GREENHOUSE_URL, SourceAts.GREENHOUSE),
            ("https://boards.greenhouse.io/northwind/jobs/1", SourceAts.GREENHOUSE),
            (LEVER_URL, SourceAts.LEVER),
        ],
    )
    def test_it_picks_the_adapter_for_the_host(self, url: str, expected: SourceAts) -> None:
        assert adapter_for(url).source is expected

    def test_an_unknown_board_names_the_ones_that_work(self) -> None:
        with pytest.raises(UnsupportedSourceError, match="greenhouse"):
            adapter_for("https://careers.example.com/jobs/1")

    def test_a_link_local_address_matches_nothing(self) -> None:
        # The cloud metadata endpoint. It reaches no adapter, so no request is made;
        # this is the structural half of the SSRF argument in router.py.
        with pytest.raises(UnsupportedSourceError):
            adapter_for("http://169.254.169.254/computeMetadata/v1/")
