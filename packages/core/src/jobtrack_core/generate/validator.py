"""The grounding validator.

ADR 13 is the argument; this is the enforcement. Four checks, all deterministic, run
over every generated bullet before it is allowed into a document.

The design principle is that this is crude on purpose. A model-based faithfulness judge
would be more nuanced and would also be wrong some percentage of the time, and a gate
that is wrong 5% of the time lets through 5% of fabrications. These checks do not have
a bad day.

They are also asymmetric on purpose: they will occasionally reject a true bullet. A
dropped true line costs the user a line they can add back by hand. A kept false one
costs them credibility in a room where they cannot take it back.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from jobtrack_core.generate.schemas import TailoredBullet
from jobtrack_core.llm.heuristic.segment import SKILL_VOCABULARY

#: Numbers a bullet may always use: small counts that are ordinary English rather than
#: claims ("a handful of teams", "two years"). The dangerous numbers are the specific
#: ones — percentages, throughput, money — and none of them are in this range.
_TRIVIAL_NUMBERS = frozenset({"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"})

#: Digits, optionally with separators, decimals, a percent sign or a magnitude suffix.
_NUMBER = re.compile(r"\d[\d,.]*\s*(?:%|percent|k\b|m\b|bn\b|b\b)?", re.IGNORECASE)
_DIGITS = re.compile(r"\d")


class Rejection(str):
    """Why a bullet was rejected. A plain string so it stores and renders as one."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class Evidence:
    """The pool of profile items a bullet is allowed to draw on.

    Only reviewed items belong here. An imported item is a model's reading of a
    document until a person confirms it, and generating from unconfirmed text would
    ground one model's output in another model's guess.
    """

    texts: dict[uuid.UUID, str]

    def combined(self, ids: list[uuid.UUID]) -> str:
        return " ".join(self.texts[item_id] for item_id in ids if item_id in self.texts)

    def known(self, item_id: uuid.UUID) -> bool:
        return item_id in self.texts


@dataclass(slots=True)
class ValidationResult:
    """Which bullets survived, and why the others did not."""

    accepted: list[TailoredBullet] = field(default_factory=list)
    rejected: list[tuple[TailoredBullet, Rejection]] = field(default_factory=list)

    @property
    def warnings(self) -> list[str]:
        return [str(reason) for _, reason in self.rejected]


def validate_bullet(
    bullet: TailoredBullet, evidence: Evidence, *, allowed_numbers: frozenset[str] = frozenset()
) -> Rejection | None:
    """Check one bullet. Returns the reason it fails, or None if it passes.

    `allowed_numbers` carries the figures from the computed fit breakdown, because the
    model is permitted to state those — they came from this system, not from it.
    """
    if not bullet.source_item_ids:
        return Rejection("cites no profile item")

    unknown = [item_id for item_id in bullet.source_item_ids if not evidence.known(item_id)]
    if unknown:
        # Either a hallucinated id, or an id belonging to someone else, or an
        # unreviewed item. All three mean the same thing: it is not grounded.
        return Rejection(f"cites {len(unknown)} profile item(s) that are not available")

    source = evidence.combined(bullet.source_item_ids)

    if invented := _invented_numbers(bullet.text, source, allowed_numbers):
        # The check that matters most. A resume is believed on its numbers, and
        # turning "reduced manual work" into "reduced manual work by 60%" fabricates
        # the single most checkable claim on the page.
        return Rejection(f"states {', '.join(sorted(invented))}, which is not in the evidence")

    if invented_skills := _invented_skills(bullet.text, source):
        return Rejection(
            f"names {', '.join(sorted(invented_skills))}, which the evidence does not mention"
        )

    return None


def validate(
    bullets: list[TailoredBullet],
    evidence: Evidence,
    *,
    allowed_numbers: frozenset[str] = frozenset(),
) -> ValidationResult:
    """Check every bullet, keeping the ones that pass."""
    result = ValidationResult()
    for bullet in bullets:
        reason = validate_bullet(bullet, evidence, allowed_numbers=allowed_numbers)
        if reason is None:
            result.accepted.append(bullet)
        else:
            result.rejected.append((bullet, reason))
    return result


def _invented_numbers(text: str, source: str, allowed: frozenset[str]) -> set[str]:
    """Numbers in the bullet that appear in neither its evidence nor the breakdown."""
    source_numbers = {_canonical(match) for match in _NUMBER.findall(source)}
    source_digits = set(_DIGITS.findall(source))

    invented: set[str] = set()
    for raw in _NUMBER.findall(text):
        canonical = _canonical(raw)
        if not canonical or canonical in _TRIVIAL_NUMBERS or canonical in allowed:
            continue
        if canonical in source_numbers:
            continue
        # A bullet may reformat a number it was given — "40M" from "40,000,000", or
        # "43%" from "43 percent". Comparing the digits catches the reformatting
        # without letting a genuinely new figure through, because a new figure almost
        # always introduces digits the evidence does not contain.
        if set(_DIGITS.findall(canonical)) <= source_digits and source_digits:
            continue
        invented.add(raw.strip())
    return invented


def _canonical(raw: str) -> str:
    """Strip separators and the percent unit so equivalent spellings compare equal.

    "87%", "87 percent" and "87" are one claim written three ways. Magnitude suffixes
    are deliberately *not* stripped: 40 and 40M are different numbers, and folding them
    together would let a bullet inflate a figure by three orders of magnitude and pass.
    """
    collapsed = re.sub(r"[,.\s]", "", raw).lower()
    return re.sub(r"(?:%|percent)$", "", collapsed)


def _invented_skills(text: str, source: str) -> set[str]:
    """Technologies named in the bullet that its evidence never mentions.

    The same vocabulary the skills gap uses, so "knows Kubernetes" means the same thing
    in both places. Only known technologies are checked: an open-ended noun check would
    reject every legitimate rephrasing.
    """
    lowered = text.lower()
    source_lowered = source.lower()
    return {
        skill
        for skill, pattern in SKILL_VOCABULARY
        if pattern.search(lowered) and not pattern.search(source_lowered)
    }
