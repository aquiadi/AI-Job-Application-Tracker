"""HTML to plain text, preserving the structure extraction depends on.

A job posting's meaning is carried substantially by its lists. "Requirements" followed
by eight `<li>` elements is eight requirements; the same text with the list flattened
into a paragraph is one wall that a model has to re-segment by guessing. So this keeps
block boundaries and list items and discards everything else.

`html.parser` from the standard library rather than a parsing dependency. The input is
an ATS's own rendered description, not arbitrary web HTML, and the tag vocabulary in
practice is small: paragraphs, lists, headings, breaks and inline emphasis.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser

#: Tags that end the current line when opened or closed.
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "table",
        "tr",
        "blockquote",
        "pre",
    }
)
#: Tags whose content is markup or styling, never prose.
_DROPPED_TAGS = frozenset({"script", "style", "head", "noscript", "svg", "iframe"})


class _Converter(HTMLParser):
    """Accumulates text, emitting `- ` for list items and blank lines between blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._dropping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROPPED_TAGS:
            self._dropping += 1
            return
        if self._dropping:
            return
        if tag == "li":
            self._parts.append("\n- ")
        elif tag == "br":
            self._parts.append("\n")
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n\n")
        elif tag == "td":
            # Cells become spaced text rather than columns. Salary tables lose their
            # alignment here, which ADR 11 records as a known cost of this stage.
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED_TAGS:
            self._dropping = max(self._dropping - 1, 0)
            return
        if self._dropping:
            return
        if tag in _BLOCK_TAGS or tag == "li":
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._dropping:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(markup: str) -> str:
    """Convert posting markup to plain text with bullets preserved.

    Entities are unescaped before parsing because the Greenhouse board API returns
    `content` as an HTML-escaped string — the markup arrives as `&lt;p&gt;` and would
    otherwise be read as literal text rather than as tags.
    """
    unescaped = html.unescape(markup)
    converter = _Converter()
    converter.feed(unescaped)
    converter.close()
    return converter.text()
