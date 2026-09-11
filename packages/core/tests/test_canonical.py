"""Normalisation and the content hash.

The hash is the key of a cache shared by every user, so two properties matter: it is
stable across the cosmetic variation that copy-and-paste introduces, and it separates
postings that are genuinely different.
"""

from __future__ import annotations

import pytest

from jobtrack_core.db.enums import SourceAts
from jobtrack_core.ingest.canonical import (
    CANONICAL_SCHEMA_VERSION,
    CanonicalPosting,
    content_hash,
    normalise,
)

BODY = "Requirements\n- Five years of Python\n- Postgres in production"


def posting(**overrides: object) -> CanonicalPosting:
    base: dict[str, object] = {
        "source_ats": SourceAts.PASTED,
        "title": "Senior Backend Engineer",
        "company": "Northwind",
        "body": BODY,
    }
    return CanonicalPosting.model_validate(base | overrides)


class TestNormalise:
    def test_it_collapses_runs_of_spaces(self) -> None:
        assert normalise("five    years   of  Python") == "five years of Python"

    def test_it_folds_unicode_that_copy_paste_introduces(self) -> None:
        # A non-breaking space from a browser and a plain space from a text editor are
        # the same posting. NFKC is what makes them hash alike.
        assert normalise("five years") == normalise("five years")  # noqa: RUF001
        assert normalise("ﬁve years") == "five years"

    def test_every_bullet_glyph_becomes_one_marker(self) -> None:
        # The literal glyphs are the point of the test.
        for glyph in ("•", "●", "▪", "·", "*", "–", "-"):  # noqa: RUF001
            assert normalise(f"{glyph} Five years") == "- Five years"

    def test_it_keeps_case(self) -> None:
        # An acronym is a signal. Lowercasing costs more in extraction quality than it
        # gains in cache hits.
        assert normalise("Experience with SQL and AWS") == "Experience with SQL and AWS"

    def test_it_keeps_paragraph_breaks_but_caps_them(self) -> None:
        assert normalise("one\n\n\n\n\ntwo") == "one\n\ntwo"


class TestContentHash:
    def test_the_same_posting_pasted_differently_hashes_alike(self) -> None:
        spaced = content_hash(title="Engineer", company="Northwind", body="•  Five  years")
        plain = content_hash(title="Engineer", company="Northwind", body="- Five years")

        assert spaced == plain

    def test_different_bodies_hash_differently(self) -> None:
        assert content_hash(title="E", company="N", body="Python") != content_hash(
            title="E", company="N", body="Java"
        )

    def test_the_title_is_part_of_the_key(self) -> None:
        # Two roles at one company share boilerplate often enough to collide on the
        # body alone, and a collision serves one posting's requirements for another.
        assert content_hash(title="Senior Engineer", company="N", body=BODY) != content_hash(
            title="Staff Engineer", company="N", body=BODY
        )

    def test_fields_cannot_be_made_to_look_like_each_other(self) -> None:
        # Length-prefixed rather than joined by a separator: with a naive join, a title
        # containing the separator could shift the field boundary and collide with a
        # different (title, company) pair.
        assert content_hash(title="ab", company="c", body=BODY) != content_hash(
            title="a", company="bc", body=BODY
        )

    def test_it_is_hex_sha256(self) -> None:
        digest = content_hash(title=None, company=None, body=BODY)

        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")


class TestCanonicalPosting:
    def test_prompt_text_puts_context_before_the_body(self) -> None:
        text = posting().prompt_text()

        assert text.startswith("Title: Senior Backend Engineer\nCompany: Northwind")
        assert text.endswith(BODY)

    def test_prompt_text_omits_headers_it_does_not_have(self) -> None:
        assert posting(title=None, company=None).prompt_text() == BODY

    def test_an_empty_body_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="body"):
            posting(body="")

    def test_it_is_immutable(self) -> None:
        # The hash is derived from these fields, so a posting that could be edited
        # after hashing would be a cache key that does not match its own content.
        with pytest.raises(ValueError, match="frozen"):
            posting().title = "Something else"

    def test_the_schema_version_is_an_integer_in_the_cache_key(self) -> None:
        assert isinstance(CANONICAL_SCHEMA_VERSION, int)
