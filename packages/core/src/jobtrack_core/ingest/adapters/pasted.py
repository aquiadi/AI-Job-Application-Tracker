"""Text a user pasted, which is the source that always works.

Every ATS adapter is a bet that a vendor keeps an undocumented endpoint stable. This
one is not, and it is the reason the product has no hard dependency on any board:
anything a user can select and copy can be ingested.

It also carries a second job. A user who pastes a *page* rather than a posting brings
navigation, cookie banners and footer text with it, and those become requirements if
nothing strips them. :func:`_strip_chrome` removes the lines that are recognisably not
prose before extraction ever sees them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jobtrack_core.db.enums import SourceAts
from jobtrack_core.ingest.adapters.base import PostingParseError
from jobtrack_core.ingest.canonical import CanonicalPosting, normalise
from jobtrack_core.ingest.html_text import html_to_text

#: Below this a paste is a title or a URL, not a posting, and extraction on it wastes
#: a call to produce nothing.
MIN_BODY_CHARS = 120

#: Lines that are site chrome wherever they appear. Matched whole, case-insensitively,
#: so a requirement that happens to contain "apply" survives.
_CHROME = re.compile(
    r"^(?:"
    r"apply(?: now| for this job)?|submit (?:your )?application|save (?:this )?job"
    r"|share|share this job|back to (?:jobs|search|results)|view all jobs"
    r"|accept(?: all)? cookies|cookie (?:policy|settings|preferences)"
    r"|privacy policy|terms (?:of use|and conditions)|sign in|log in|create an account"
    r"|skip to (?:main )?content|menu|home|careers|©.*|all rights reserved.*"
    r")$",
    re.IGNORECASE,
)

_LOOKS_LIKE_MARKUP = re.compile(r"<(?:p|div|ul|ol|li|br|h[1-6]|span|section)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PastedAdapter:
    """Normalises pasted text. Never fetches anything."""

    @property
    def source(self) -> SourceAts:
        return SourceAts.PASTED

    def matches(self, url: str) -> bool:
        # Pasted text is chosen explicitly by the caller, never by URL routing.
        return False

    def api_url(self, url: str) -> str:
        raise PostingParseError("pasted postings are not fetched")

    def parse(
        self,
        payload: bytes,
        *,
        source_url: str,
        title: str | None = None,
        company: str | None = None,
    ) -> CanonicalPosting:
        text = payload.decode("utf-8", errors="replace")
        # A paste out of a browser's rendered view is text; a paste out of "view
        # source" or a developer tool is markup. Both happen, so both are handled.
        if _LOOKS_LIKE_MARKUP.search(text):
            text = html_to_text(text)

        body = normalise(_strip_chrome(text))
        if len(body) < MIN_BODY_CHARS:
            raise PostingParseError(
                f"that is {len(body)} characters; a job posting needs at least "
                f"{MIN_BODY_CHARS}. Paste the full description."
            )

        # Title and company are taken from the caller when given and are never guessed
        # from the first line of a paste. The person pasting knows the role they are
        # looking at; inferring it is wrong often enough to be worse than asking.
        return CanonicalPosting(
            source_ats=SourceAts.PASTED,
            source_url=source_url or None,
            title=(title or "").strip() or None,
            company=(company or "").strip() or None,
            body=body,
        )


def _strip_chrome(text: str) -> str:
    return "\n".join(line for line in text.split("\n") if not _CHROME.match(line.strip()))
