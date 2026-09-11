"""Greenhouse job boards.

Two host spellings are in circulation: `boards.greenhouse.io` is the older form and
`job-boards.greenhouse.io` is what Greenhouse's own `absolute_url` returns as of
2026-09-11. Both are accepted because users paste whichever their browser shows.

The board API returns the description in `content` as an HTML-escaped string, so the
markup arrives as `&lt;p&gt;` and has to be unescaped before it can be parsed as HTML.
:func:`html_to_text` does that unescaping.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jobtrack_core.db.enums import SourceAts
from jobtrack_core.ingest.adapters.base import PostingParseError
from jobtrack_core.ingest.canonical import CanonicalPosting, normalise
from jobtrack_core.ingest.html_text import html_to_text

_URL = re.compile(
    r"^https?://(?:job-)?boards\.greenhouse\.io/(?P<board>[^/]+)/jobs/(?P<job_id>\d+)",
    re.IGNORECASE,
)
_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=false"


@dataclass(frozen=True, slots=True)
class GreenhouseAdapter:
    """Reads the public Greenhouse board API."""

    @property
    def source(self) -> SourceAts:
        return SourceAts.GREENHOUSE

    def matches(self, url: str) -> bool:
        return _URL.match(url.strip()) is not None

    def api_url(self, url: str) -> str:
        match = _URL.match(url.strip())
        if match is None:
            raise PostingParseError(f"not a Greenhouse job URL: {url}")
        return _API.format(board=match["board"], job_id=match["job_id"])

    def parse(self, payload: bytes, *, source_url: str) -> CanonicalPosting:
        try:
            document: Any = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PostingParseError("Greenhouse returned something that is not JSON") from exc

        if not isinstance(document, dict) or "content" not in document:
            raise PostingParseError("Greenhouse returned no posting content")

        body = normalise(html_to_text(str(document.get("content") or "")))
        if not body:
            raise PostingParseError("the Greenhouse posting has an empty description")

        location = document.get("location")
        return CanonicalPosting(
            source_ats=SourceAts.GREENHOUSE,
            # `absolute_url` is canonical and deduplicates the two host spellings; the
            # pasted URL is the fallback only when the field is absent.
            source_url=str(document.get("absolute_url") or source_url),
            title=_text(document.get("title")),
            company=_text(document.get("company_name")),
            location=_text(location.get("name")) if isinstance(location, dict) else None,
            posted_at=_timestamp(document.get("first_published") or document.get("updated_at")),
            body=body,
        )


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _timestamp(value: object) -> datetime | None:
    """Greenhouse sends ISO 8601 with a numeric offset, which `fromisoformat` reads."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
