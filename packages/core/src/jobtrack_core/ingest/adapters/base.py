"""What every source adapter has to provide.

An adapter does two things and nothing else: recognise a URL, and turn the bytes that
URL yields into a :class:`CanonicalPosting`. It performs no network call itself, which
is what lets every adapter be tested against a recorded payload with no network and no
mocking, and what keeps fetching policy — timeouts, redirects, size limits — in one
place instead of repeated per source.
"""

from __future__ import annotations

from typing import Protocol

from jobtrack_core.db.enums import SourceAts
from jobtrack_core.ingest.canonical import CanonicalPosting


class IngestError(Exception):
    """Ingestion failed in a way worth showing the user."""


class UnsupportedSourceError(IngestError):
    """The URL is not one of the boards this system can read."""


class PostingParseError(IngestError):
    """The payload arrived but did not contain a posting.

    Usually a deleted listing: an ATS answers 200 with an error document rather than
    404 more often than one would hope.
    """


class PostingAdapter(Protocol):
    """Recognises a job board and normalises its payload."""

    @property
    def source(self) -> SourceAts: ...

    def matches(self, url: str) -> bool:
        """True if this adapter can read the URL."""
        ...

    def api_url(self, url: str) -> str:
        """The URL to actually fetch, which is rarely the one the user pasted.

        Job boards serve a JavaScript shell to browsers and JSON to their own API. The
        JSON is a documented, stable contract; the rendered page is neither.
        """
        ...

    def parse(self, payload: bytes, *, source_url: str) -> CanonicalPosting:
        """Turn the fetched bytes into the canonical shape."""
        ...
