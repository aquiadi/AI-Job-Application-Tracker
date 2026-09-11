"""Getting text out of an uploaded resume.

Only the text layer. A scanned resume is an image of a document and has no text layer,
and the honest response to one is to say so — running OCR would be a second dependency,
a second failure mode, and a quality floor low enough that the items it produced would
have to be reviewed word by word anyway.

The user reviews every imported item regardless, which is what `reviewed` on
`profile_items` is for. Nothing unreviewed is ever cited by generated content or
counted by the fit score.
"""

from __future__ import annotations

import io
import re

from pypdf import PdfReader
from pypdf.errors import PyPdfError

#: Above this a PDF is not a resume. Parsing it would spend memory and produce items
#: nobody wants to review.
MAX_PAGES = 15
MAX_BYTES = 10 * 1024 * 1024
#: Below this there is no usable text layer, which in practice means a scan.
MIN_TEXT_CHARS = 200

_LIGATURES = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"})
_BULLET = re.compile(r"^[\s]*[•●▪‣⁃·*+–—-]\s*", re.M)  # noqa: RUF001
_SPACES = re.compile(r"[ \t ]+")  # noqa: RUF001


class ResumeReadError(Exception):
    """The upload could not be read as a resume. The message is shown to the user."""


def extract_text(data: bytes) -> str:
    """Pull the text layer out of a PDF, preserving line and bullet structure."""
    if len(data) > MAX_BYTES:
        raise ResumeReadError(
            f"that file is {len(data) // 1_048_576} MB; the limit is {MAX_BYTES // 1_048_576} MB"
        )

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ResumeReadError("that PDF is password protected. Save an unlocked copy.")
        if len(reader.pages) > MAX_PAGES:
            raise ResumeReadError(
                f"that PDF has {len(reader.pages)} pages; the limit is {MAX_PAGES}"
            )
        pages = [page.extract_text() or "" for page in reader.pages]
    except ResumeReadError:
        raise
    except (PyPdfError, ValueError, OSError) as exc:
        raise ResumeReadError("that file could not be read as a PDF") from exc

    text = _normalise("\n".join(pages))
    if len(text) < MIN_TEXT_CHARS:
        raise ResumeReadError(
            "that PDF has no text layer, which usually means it is a scan. "
            "Export a PDF from your editor, or paste the text instead."
        )
    return text


def _normalise(text: str) -> str:
    text = text.translate(_LIGATURES)
    text = _BULLET.sub("- ", text)
    text = _SPACES.sub(" ", text)
    lines = [line.strip() for line in text.replace("\r", "").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
