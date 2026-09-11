"""Rule-based resume parsing. The offline counterpart to the import prompt.

Resumes are less conventional than job postings, so this is a weaker baseline than
:mod:`segment` — a two-column layout arrives from the PDF text layer already
interleaved, and no amount of line rules recovers it. It handles the common case: a
section heading, then roles, then bullets under each.

It never guesses contact details. There is nowhere in :class:`ParsedResume` to put
them, which is the same structural guarantee the prompt relies on.
"""

from __future__ import annotations

import re
from datetime import date

from jobtrack_core.db.enums import ProfileItemKind
from jobtrack_core.profile.schemas import MAX_ITEM_CHARS, ParsedItem, ParsedResume

_BULLET = re.compile(r"^\s*[-*]\s+(?P<text>.+)$")
#: A heading is a short line with no sentence punctuation, usually capitalised.
_HEADING = re.compile(r"^\s*(?P<text>[A-Za-z][^.!?]{2,48}?):?\s*$")

_SECTIONS: tuple[tuple[re.Pattern[str], ProfileItemKind], ...] = (
    (
        re.compile(r"\b(experience|employment|work history|professional)", re.I),
        ProfileItemKind.EXPERIENCE_BULLET,
    ),
    (re.compile(r"\b(project|portfolio|open source)", re.I), ProfileItemKind.PROJECT),
    (re.compile(r"\b(skill|technolog|technical|tool|competenc)", re.I), ProfileItemKind.SKILL),
    (
        re.compile(r"\b(education|degree|certification|qualification|licen[cs]e)", re.I),
        ProfileItemKind.EDUCATION,
    ),
)

#: "Staff Engineer at Northwind", "Northwind — Staff Engineer", "Northwind, Staff Engineer".
_ROLE_LINE = re.compile(
    r"^(?P<left>[A-Z][\w&.,' ]{2,60}?)\s*"
    r"(?:\||,|-|–|—|\bat\b)\s*"  # noqa: RUF001 - both dash characters are matched on purpose
    r"(?P<right>[A-Z][\w&.,' ]{2,60})",
)
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
#: Comma or bullet separated technology lists, which is how a Skills section reads.
_SEPARATORS = re.compile(r"[,;|•·]")

#: A skills line longer than this is prose, not a list.
MAX_SKILL_CHARS = 60


def parse(text: str) -> ParsedResume:
    """Segment resume text into reviewable items."""
    items: list[ParsedItem] = []
    kind = ProfileItemKind.EXPERIENCE_BULLET
    organisation: str | None = None
    role: str | None = None
    started: date | None = None
    ended: date | None = None
    headline: str | None = None

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue

        bullet = _BULLET.match(raw)
        if bullet is not None:
            body = bullet["text"].strip()[:MAX_ITEM_CHARS]
            if len(body) < 3:
                continue
            if kind is ProfileItemKind.SKILL:
                items.extend(_skills(body))
            else:
                items.append(
                    ParsedItem(
                        text=body,
                        kind=kind,
                        organisation=organisation,
                        role=role,
                        started_on=started,
                        ended_on=ended,
                    )
                )
            continue

        if (heading := _HEADING.match(line)) and (found := _section(heading["text"])):
            kind, organisation, role, started, ended = found, None, None, None, None
            continue

        if kind is ProfileItemKind.SKILL and len(line) <= MAX_SKILL_CHARS * 4:
            items.extend(_skills(line))
            continue

        if match := _ROLE_LINE.match(line):
            # Which half is the employer is genuinely ambiguous in a resume, and
            # guessing wrong swaps two labels the user can see and fix. Both are
            # recorded in the order written and shown for review.
            organisation, role = match["right"].strip(), match["left"].strip()
            started, ended = _dates(line)
            continue

        is_opening = kind is ProfileItemKind.EXPERIENCE_BULLET
        if headline is None and is_opening and 20 <= len(line) <= 200:
            headline = line

    return ParsedResume(headline=headline, items=items)


def _section(heading: str) -> ProfileItemKind | None:
    for pattern, kind in _SECTIONS:
        if pattern.search(heading):
            return kind
    return None


def _skills(line: str) -> list[ParsedItem]:
    """One item per named technology, not one per line.

    A Skills section is a list, and storing it as a single row would give the fit score
    one vector covering thirty unrelated technologies — which matches every requirement
    weakly and none of them well.
    """
    return [
        ParsedItem(text=part, kind=ProfileItemKind.SKILL)
        for raw in _SEPARATORS.split(line)
        if 2 <= len(part := raw.strip()) <= MAX_SKILL_CHARS
    ]


def _dates(line: str) -> tuple[date | None, date | None]:
    """January of each year mentioned. Month precision is not recoverable here."""
    years = [int(match.group()) for match in _YEAR.finditer(line)]
    if not years:
        return None, None
    start = date(min(years), 1, 1)
    return (start, date(max(years), 1, 1) if max(years) != min(years) else None)
