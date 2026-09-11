"""Choosing an adapter for a URL, and fetching what it asks for.

The fetch is worth reading carefully, because it is the one place this system makes an
outbound request to an address influenced by user input.

It is not a server-side request forgery risk, and the reason is structural rather than
a filter: the URL that gets fetched is never the URL the user supplied. An adapter's
`matches` anchors on a specific host, and its `api_url` builds a new URL from a
constant template with only the board slug and posting id interpolated. A user who
pastes `http://169.254.169.254/` matches no adapter and is rejected before any request
is made. Redirects are disabled for the same reason: a 302 from a board API would
otherwise reintroduce an arbitrary destination.
"""

from __future__ import annotations

import httpx

from jobtrack_core.ingest.adapters.base import (
    IngestError,
    PostingAdapter,
    PostingParseError,
    UnsupportedSourceError,
)
from jobtrack_core.ingest.adapters.greenhouse import GreenhouseAdapter
from jobtrack_core.ingest.adapters.lever import LeverAdapter
from jobtrack_core.ingest.adapters.pasted import PastedAdapter
from jobtrack_core.ingest.canonical import CanonicalPosting

#: Ordered, though no two currently overlap. Pasted text is deliberately absent: it is
#: selected by the caller, never matched from a URL.
ADAPTERS: tuple[PostingAdapter, ...] = (GreenhouseAdapter(), LeverAdapter())

PASTED = PastedAdapter()

#: A posting is tens of kilobytes. Anything larger is a board index or an error page,
#: and reading it into memory to discover that is the failure mode this prevents.
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 20.0

_HEADERS = {"Accept": "application/json", "User-Agent": "jobtrack/0.1 (+job posting ingestion)"}


def adapter_for(url: str) -> PostingAdapter:
    """Find the adapter that reads this URL, or say which boards are supported."""
    for adapter in ADAPTERS:
        if adapter.matches(url):
            return adapter
    supported = ", ".join(sorted(adapter.source.value for adapter in ADAPTERS))
    raise UnsupportedSourceError(
        f"no adapter reads that URL. Supported boards: {supported}. "
        "Paste the description text instead."
    )


def supported_hosts() -> tuple[str, ...]:
    """Host names the interface can advertise as pasteable."""
    return ("boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co")


async def fetch_posting(url: str, *, client: httpx.AsyncClient | None = None) -> CanonicalPosting:
    """Fetch and normalise a posting from a supported board."""
    adapter = adapter_for(url)
    target = adapter.api_url(url)

    owned = client is None
    http = client or httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers=_HEADERS,
    )
    try:
        response = await http.get(target)
    except httpx.HTTPError as exc:
        raise IngestError(f"could not reach {adapter.source.value}: {exc}") from exc
    finally:
        if owned:
            await http.aclose()

    if response.status_code == httpx.codes.NOT_FOUND:
        raise PostingParseError("that posting no longer exists on the board")
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise IngestError(f"{adapter.source.value} answered {response.status_code}")
    if len(response.content) > MAX_PAYLOAD_BYTES:
        raise IngestError("that posting is implausibly large; paste the description instead")

    return adapter.parse(response.content, source_url=url)


def parse_pasted(
    text: str,
    *,
    source_url: str = "",
    title: str | None = None,
    company: str | None = None,
) -> CanonicalPosting:
    """Normalise pasted text. No network, no routing."""
    return PASTED.parse(text.encode("utf-8"), source_url=source_url, title=title, company=company)
