"""Rule-based resume parsing. The offline counterpart to the import prompt.

Resumes follow a convention loosely, which makes this a weaker baseline than
:mod:`segment`: a two-column layout arrives from the PDF text layer already
interleaved, and no line rules recover it. What it does handle is the common shape — a
contact block, then sections, then roles, then bullets under each — and it handles the
parts that matter most for the fit score, which are the bullets and the skills.

Two behaviours are load-bearing rather than incidental.

**The contact block is detected and discarded.** Not stripped from the output after the
fact — never turned into an item at all. Contact details belong on their own columns and
must never reach a model, and the strongest version of that rule is that nothing here
can produce them.

**Which half of "Northwind — Staff Engineer" is the employer is decided by vocabulary,
not by position.** Resumes write it both ways round. Guessing by position is wrong about
half the time, and the two labels are visible to the user, so being wrong is worse than
being unsure.
"""

from __future__ import annotations

import re
from datetime import date

from jobtrack_core.db.enums import ProfileItemKind
from jobtrack_core.profile.schemas import (
    MAX_ITEM_CHARS,
    MIN_PROSE_CHARS,
    ParsedItem,
    ParsedResume,
)

_BULLET = re.compile(r"^\s*[-*•●▪·]\s+(?P<text>.+)$")
#: A heading is a short line with no sentence punctuation.
_HEADING = re.compile(r"^\s*(?P<text>[A-Za-z][^.!?]{2,48}?):?\s*$")

_SECTIONS: tuple[tuple[re.Pattern[str], ProfileItemKind], ...] = (
    (
        re.compile(r"\b(experience|employment|work history|professional)", re.I),
        ProfileItemKind.EXPERIENCE_BULLET,
    ),
    (re.compile(r"\b(project|portfolio|open source)", re.I), ProfileItemKind.PROJECT),
    (re.compile(r"\b(skill|technolog|technical|tool|competenc)", re.I), ProfileItemKind.SKILL),
    (
        re.compile(r"\b(education|degree|certification|qualification|licen[cs]e|award)", re.I),
        ProfileItemKind.EDUCATION,
    ),
)

# Contact detection. Each pattern alone is weak; together they are decisive, and the
# cost of a false positive (one dropped line near the top) is far lower than the cost
# of a false negative (a phone number becoming a profile item).
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
_URL = re.compile(
    r"\b(?:https?://|www\.)|(?:linkedin|github|gitlab|twitter|x)\.com/|\b[\w-]+\.(?:dev|io|me)\b",
    re.I,
)
#: How far into the document a line can still be part of the contact block.
CONTACT_WINDOW = 8

#: Words that identify the role half of a "Company — Title" line. Checked against both
#: halves, so the answer comes from the words rather than from which side they are on.
_ROLE_WORDS = re.compile(
    r"\b(engineer|developer|scientist|analyst|manager|director|designer|architect"
    r"|consultant|lead|head|officer|president|founder|intern|associate|specialist"
    r"|administrator|researcher|programmer|strategist|coordinator|advisor)\b",
    re.I,
)
# The comma is written without a leading space because "Helios Data, Senior
# Engineer" is at least as common as a spaced dash, and requiring symmetry
# silently left every such role unrecognised — which meant the role above it
# stayed attached to bullets that belonged to a different employer.
_SEPARATOR = re.compile(
    r"\s*,\s+"  # "Helios Data, Senior Engineer" — no space before the comma
    r"|\s+(?:-|—|–|\||·|•)\s+"  # noqa: RUF001 - a spaced dash of any kind
    r"|\s+\bat\b\s+",  # "Senior Engineer at Helios Data"
    re.I,
)

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_DATE_RANGE = re.compile(
    rf"(?P<m1>{_MONTH})?\s*(?P<y1>(?:19|20)\d{{2}})\s*(?:-|–|—|to|until)\s*"  # noqa: RUF001
    rf"(?:(?P<present>present|current|now)|(?:(?P<m2>{_MONTH})?\s*(?P<y2>(?:19|20)\d{{2}})))",
    re.I,
)
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

#: Comma, bullet or slash separated technology lists, which is how a skills section reads.
_SKILL_SEPARATORS = re.compile(r"[,;|•·/]")
#: Longer than this and it is prose describing a skill, not the skill's name.
MAX_SKILL_CHARS = 40
#: A skills line longer than this is a sentence, so it is not split into skills at all.
MAX_SKILL_LINE_CHARS = 400


def parse(text: str) -> ParsedResume:
    """Segment resume text into reviewable items."""
    lines = [line.rstrip() for line in text.replace("\r", "").split("\n")]
    contact_until = _contact_block_end(lines)

    items: list[ParsedItem] = []
    headline: str | None = None
    kind = ProfileItemKind.EXPERIENCE_BULLET
    seen_heading = False
    organisation: str | None = None
    role: str | None = None
    started: date | None = None
    ended: date | None = None

    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or index < contact_until:
            continue

        if bullet := _BULLET.match(raw):
            body = bullet["text"].strip()[:MAX_ITEM_CHARS]
            if kind is ProfileItemKind.SKILL:
                items.extend(_skills(body))
            elif len(body) >= MIN_PROSE_CHARS:
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

        if (heading := _HEADING.match(line)) and (section := _section(heading["text"])):
            kind, seen_heading = section, True
            organisation = role = None
            started = ended = None
            continue

        # A date-only line belongs to the role above it. Resumes put the range on its
        # own line at least as often as on the heading, and losing it means every
        # bullet under that role carries no dates.
        if (found := _date_range(line)) != (None, None) and len(line) <= 60:
            started, ended = found
            continue

        if kind is ProfileItemKind.SKILL and len(line) <= MAX_SKILL_LINE_CHARS:
            items.extend(_skills(line))
            continue

        if kind is ProfileItemKind.EDUCATION and len(line) >= MIN_PROSE_CHARS:
            # An education line is the item. There is rarely a bullet under it.
            qualified = _date_range(line)
            items.append(
                ParsedItem(
                    text=line[:MAX_ITEM_CHARS],
                    kind=kind,
                    started_on=qualified[0],
                    ended_on=qualified[1],
                )
            )
            continue

        if parsed := _role_line(line):
            organisation, role = parsed
            found = _date_range(line)
            if found != (None, None):
                started, ended = found
            continue

        # Before any heading, an ordinary line is the summary the resume opens with.
        if headline is None and not seen_heading and 20 <= len(line) <= 200:
            headline = line

    return ParsedResume(headline=headline, items=items)


def _contact_block_end(lines: list[str]) -> int:
    """Index of the first line after the contact block.

    Scans only the top of the document, and stops at the first section heading. A
    "contact" line further down is a coincidence; up here it is the header.
    """
    last_contact = -1
    for index, raw in enumerate(lines[:CONTACT_WINDOW]):
        line = raw.strip()
        if not line:
            continue
        if (heading := _HEADING.match(line)) and _section(heading["text"]):
            break
        if is_contact_line(line):
            last_contact = index
    return last_contact + 1


def is_contact_line(line: str) -> bool:
    """True if the line is contact details rather than content.

    The name line is caught too: a short line of capitalised words with no verb, sitting
    above an email, is a name. It is dropped rather than kept, because a name in a
    profile item would travel to a model.
    """
    if _EMAIL.search(line) or _URL.search(line):
        return True
    if _PHONE.search(line) and len(line) <= 80:
        return True
    words = line.split()
    if 1 <= len(words) <= 4 and all(word[:1].isupper() or word.isupper() for word in words):
        # A name, or an all-caps name line. Section headings are excluded before this
        # is reached, so this does not swallow "EXPERIENCE".
        return not _section(line)
    return False


def _section(heading: str) -> ProfileItemKind | None:
    for pattern, kind in _SECTIONS:
        if pattern.search(heading):
            return kind
    return None


def _role_line(line: str) -> tuple[str | None, str | None] | None:
    """Split "Company — Title" into (organisation, role), whichever way round it is."""
    parts = [part.strip(" ,–—-·") for part in _SEPARATOR.split(line, maxsplit=1)]  # noqa: RUF001
    if len(parts) != 2 or not all(parts):
        return None
    left, right = parts
    if len(left) > 80 or len(right) > 80:
        return None

    left_is_role = bool(_ROLE_WORDS.search(left))
    right_is_role = bool(_ROLE_WORDS.search(right))
    if left_is_role == right_is_role:
        # Both or neither look like a title. Recording it in written order and letting
        # the user correct it beats inventing a confidence this does not have.
        return left, right
    return (right, left) if left_is_role else (left, right)


def _date_range(line: str) -> tuple[date | None, date | None]:
    """Read "Jan 2021 - Present" or "2018 - 2020" into two dates.

    Month precision where the resume gives a month, January otherwise. A current role
    has a null end date, which is what `ended_on` means everywhere else in the system.
    """
    match = _DATE_RANGE.search(line)
    if match is None:
        return None, None

    start = _as_date(match["y1"], match["m1"])
    if match["present"]:
        return start, None
    return start, _as_date(match["y2"], match["m2"])


def _as_date(year: str | None, month: str | None) -> date | None:
    if not year:
        return None
    number = _MONTHS.get((month or "")[:3].lower(), 1)
    try:
        return date(int(year), number, 1)
    except ValueError:
        return None


def _skills(line: str) -> list[ParsedItem]:
    """One item per named technology, not one per line.

    A skills section is a list, and storing it as a single row would give the fit score
    one vector covering thirty unrelated technologies — which matches every requirement
    weakly and none of them well.
    """
    found: list[ParsedItem] = []
    for raw in _SKILL_SEPARATORS.split(line):
        skill = raw.strip(" .")
        if skill and len(skill) <= MAX_SKILL_CHARS:
            found.append(ParsedItem(text=skill, kind=ProfileItemKind.SKILL))
    return found
