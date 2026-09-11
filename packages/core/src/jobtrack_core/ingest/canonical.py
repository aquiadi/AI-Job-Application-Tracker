"""The one shape every posting is reduced to before anything else looks at it.

ADR 11 records why this stage exists. The short version: extraction should not know
what Greenhouse is, and the global extraction cache only pays for itself if two people
saving the same posting compute the same key.

Nothing in this module touches the network or a model. Adapters are pure functions over
bytes, which is what makes them testable against a recorded payload and what keeps
user data out of :func:`content_hash` — a `CanonicalPosting` has nowhere to put any.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from jobtrack_core.db.enums import SourceAts

#: Compiled into every cache key and stored on every `jobs` row. Bumping it
#: invalidates cached extractions by construction rather than by a manual purge, so a
#: change to the extraction schema cannot serve a stale shape to a new reader.
CANONICAL_SCHEMA_VERSION = 1

# The characters below are the data, not a typo: these are exactly the glyphs that
# survive a copy-paste or an HTML-to-text pass, which is why they are matched
# literally rather than escaped into unreadability.
_WHITESPACE = re.compile(r"[ \t ]+")  # noqa: RUF001
_BLANK_LINES = re.compile(r"\n{3,}")
#: Bullet glyphs that survive HTML-to-text conversion, normalised to a single marker so
#: the same posting hashes identically whether it came from Greenhouse or a paste.
_BULLETS = re.compile(r"^[\s]*[•●▪‣⁃·*+–—-]\s+", re.M)  # noqa: RUF001


class CanonicalPosting(BaseModel):
    """A posting, source-independent.

    `body` is plain text with list structure preserved as `- ` bullets. That
    compromise is deliberate: bullets carry most of the requirement structure in a job
    posting, and dropping them costs extraction quality, while keeping the surrounding
    markup costs input tokens and invites the model to read navigation as requirements.
    """

    model_config = ConfigDict(frozen=True)

    source_ats: SourceAts
    source_url: str | None = None
    title: str | None = None
    company: str | None = None
    location: str | None = None
    posted_at: datetime | None = None
    body: str = Field(min_length=1)

    @property
    def content_hash(self) -> str:
        """The global cache key for this posting."""
        return content_hash(title=self.title, company=self.company, body=self.body)

    def prompt_text(self) -> str:
        """What extraction is shown. Headers first so the model has context up front."""
        header = "\n".join(
            f"{label}: {value}"
            for label, value in (
                ("Title", self.title),
                ("Company", self.company),
                ("Location", self.location),
            )
            if value
        )
        return f"{header}\n\n{self.body}".strip() if header else self.body


def normalise(text: str) -> str:
    """Collapse the variation that does not change what a posting says.

    NFKC first, because the same posting copied from a browser and from a PDF differs
    in non-breaking spaces and ligatures without differing in content. Case is
    preserved: an acronym is a signal, and lowercasing costs more in extraction quality
    than it gains in cache hits.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _BULLETS.sub("- ", text)
    text = _WHITESPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def content_hash(*, title: str | None, company: str | None, body: str) -> str:
    """A stable key over a posting's content.

    The title and company join the body because two different roles at one company can
    share boilerplate long enough to collide on the body alone, and a collision here
    serves one posting's requirements for another.

    The fields are length-prefixed rather than joined with a separator, so a title
    containing the separator cannot be made to look like a different field split.
    """
    digest = hashlib.sha256()
    for part in (normalise(title or ""), normalise(company or ""), normalise(body)):
        encoded = part.encode("utf-8")
        digest.update(str(len(encoded)).encode("ascii"))
        digest.update(b"\x00")
        digest.update(encoded)
    return digest.hexdigest()
