"""Lever job boards.

Lever splits a posting across several fields rather than returning one description:
`description` is the opening prose, `lists` is an array of titled sections each holding
its own `<li>` markup, and `additional` is the closing boilerplate. Reassembling them
in that order reproduces what the page shows.

The section titles in `lists` are worth keeping. "Required Qualifications" ahead of a
block of bullets tells extraction that those bullets are must-haves, and dropping the
heading loses a signal no amount of prompt wording recovers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jobtrack_core.db.enums import SourceAts
from jobtrack_core.ingest.adapters.base import PostingParseError
from jobtrack_core.ingest.canonical import CanonicalPosting, normalise
from jobtrack_core.ingest.html_text import html_to_text

_URL = re.compile(
    r"^https?://jobs\.(?:eu\.)?lever\.co/(?P<company>[^/]+)/(?P<posting_id>[0-9a-f-]{8,})",
    re.IGNORECASE,
)
_API = "https://api.lever.co/v0/postings/{company}/{posting_id}?mode=json"


@dataclass(frozen=True, slots=True)
class LeverAdapter:
    """Reads the public Lever postings API."""

    @property
    def source(self) -> SourceAts:
        return SourceAts.LEVER

    def matches(self, url: str) -> bool:
        return _URL.match(url.strip()) is not None

    def api_url(self, url: str) -> str:
        match = _URL.match(url.strip())
        if match is None:
            raise PostingParseError(f"not a Lever job URL: {url}")
        return _API.format(company=match["company"], posting_id=match["posting_id"])

    def parse(self, payload: bytes, *, source_url: str) -> CanonicalPosting:
        try:
            document: Any = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PostingParseError("Lever returned something that is not JSON") from exc

        # A single-posting request returns an object; a board request returns an array.
        # Accepting both means a board URL degrades to its first posting rather than
        # failing on a type error the user cannot act on.
        if isinstance(document, list):
            if not document:
                raise PostingParseError("the Lever board returned no postings")
            document = document[0]
        if not isinstance(document, dict):
            raise PostingParseError("Lever returned an unexpected document")
        if document.get("ok") is False:
            raise PostingParseError(f"Lever: {document.get('error') or 'posting not found'}")

        body = normalise("\n\n".join(_sections(document)))
        if not body:
            raise PostingParseError("the Lever posting has an empty description")

        categories = document.get("categories")
        categories = categories if isinstance(categories, dict) else {}

        return CanonicalPosting(
            source_ats=SourceAts.LEVER,
            source_url=_text(document.get("hostedUrl")) or source_url,
            title=_text(document.get("text")),
            # Lever has no company field: the board owner is the company, and the
            # hosted URL is where its slug lives.
            company=_company(_text(document.get("hostedUrl")) or source_url),
            location=_text(categories.get("location")),
            posted_at=_timestamp(document.get("createdAt")),
            body=body,
        )


def _sections(document: dict[str, Any]) -> list[str]:
    """Opening prose, then each titled list, then the closing text."""
    parts: list[str] = []

    for key in ("descriptionPlain", "description"):
        if raw := _text(document.get(key)):
            parts.append(raw if key.endswith("Plain") else html_to_text(raw))
            break

    entries = document.get("lists")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        heading = _text(entry.get("text"))
        content = _text(entry.get("content"))
        if content is None:
            continue
        parts.append(f"{heading}\n{html_to_text(content)}" if heading else html_to_text(content))

    for key in ("additionalPlain", "additional"):
        if raw := _text(document.get(key)):
            parts.append(raw if key.endswith("Plain") else html_to_text(raw))
            break

    # `workplaceType` is remote/hybrid/onsite and often appears nowhere in the prose.
    # It goes into the body rather than onto a column so that extraction remains the
    # only thing that decides field values, per ADR 11.
    if workplace := _text(document.get("workplaceType")):
        parts.append(f"Workplace type: {workplace}")

    return [part for part in parts if part.strip()]


def _company(hosted_url: str) -> str | None:
    match = _URL.match(hosted_url)
    if match is None:
        return None
    # Slugs are lowercase and hyphenated; title-casing reads better on a board and is
    # no less accurate than the slug itself.
    return match["company"].replace("-", " ").title()


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _timestamp(value: object) -> datetime | None:
    """Lever sends `createdAt` as epoch milliseconds."""
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
