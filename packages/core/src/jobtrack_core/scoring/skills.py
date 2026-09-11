"""Which named technologies a posting asks for, and which ones you can show.

This is deliberately not the fit score. The score is about requirements — whole
statements of capability, compared by meaning. This is about *tokens*: "Kubernetes",
"gRPC", "CPA", "Series 7". Those are the terms an applicant tracking system filters on
and a recruiter scans for, and they behave differently from prose: a near miss is a
miss. Someone who has used Docker and Terraform has not used Kubernetes, and any
matching that blurs the two is telling the user something untrue.

So the match here is exact, on word boundaries, case-insensitively, against the text of
every reviewed profile item — not only the ones filed as skills, because "Built the
streaming ingestion layer on Kafka" is evidence of Kafka whether or not Kafka also
appears in a skills list. The item that proves it is returned alongside, because a
claim the user cannot see the basis for is one they cannot check.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobtrack_core.db.enums import ProfileItemKind
from jobtrack_core.db.models import Job, ProfileItem

#: One reviewed profile item: its id, its text, and what kind of item it is.
type Evidence = tuple[uuid.UUID, str, ProfileItemKind]


@dataclass(frozen=True, slots=True)
class SkillMatch:
    """A skill the posting names and the profile can show."""

    skill: str
    #: The profile item that proves it, so the claim is checkable.
    evidence: str
    evidence_item_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class SkillGap:
    """What the posting asks for, split by whether the profile shows it."""

    have: tuple[SkillMatch, ...]
    lack: tuple[str, ...]
    #: Named in the profile and not asked for by this posting. Not a weakness — it is
    #: what the user might reasonably cut from a tailored resume for this role.
    unused: tuple[str, ...]

    @property
    def asked_for(self) -> int:
        return len(self.have) + len(self.lack)

    @property
    def coverage(self) -> float:
        """Share of named skills the profile can evidence, 0 to 1."""
        return len(self.have) / self.asked_for if self.asked_for else 0.0


async def skill_gap(session: AsyncSession, *, job_id: uuid.UUID) -> SkillGap:
    """Compare the posting's named technologies against reviewed profile items."""
    job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        return SkillGap(have=(), lack=(), unused=())

    rows = (
        await session.execute(
            select(ProfileItem.id, ProfileItem.text, ProfileItem.kind).where(
                ProfileItem.reviewed.is_(True)
            )
        )
    ).all()
    items: list[Evidence] = [(row[0], row[1], row[2]) for row in rows]

    have: list[SkillMatch] = []
    lack: list[str] = []
    matched_texts: set[str] = set()

    for skill in _unique(job.hard_skills):
        found = _find(skill, items)
        if found is None:
            lack.append(skill)
            continue
        item_id, text = found
        have.append(SkillMatch(skill=skill, evidence=text, evidence_item_id=item_id))
        matched_texts.add(text.strip().casefold())

    # Only skill-kind items count as unused. An experience bullet is not a skill the
    # user "has spare"; it is a sentence that happens not to mention this posting.
    wanted = {skill.casefold() for skill in job.hard_skills}
    unused = _unique(
        text.strip()
        for _, text, kind in items
        if kind is ProfileItemKind.SKILL and text.strip().casefold() not in wanted
    )

    return SkillGap(have=tuple(have), lack=tuple(lack), unused=tuple(unused))


def _find(skill: str, items: Sequence[Evidence]) -> tuple[uuid.UUID, str] | None:
    """The shortest item mentioning this skill, preferring a dedicated skill row.

    Shortest because a one-word skill row is a cleaner citation than a forty-word
    bullet that happens to contain the term.
    """
    pattern = _pattern(skill)
    matches = [(item_id, text) for item_id, text, _ in items if pattern.search(text)]
    if not matches:
        return None
    return min(matches, key=lambda match: len(match[1]))


def _pattern(skill: str) -> re.Pattern[str]:
    """Match a technology name on its own, not inside a longer word.

    `\\b` is wrong here: it is defined against `\\w`, so `\\bc++\\b` never matches and
    `\\bgo\\b` matches the "go" in "Django" only by luck of the surrounding characters.
    Lookarounds against the characters that can legitimately appear inside a technology
    name handle `c++`, `.net` and `node.js` correctly.
    """
    escaped = re.escape(skill.strip())
    return re.compile(
        rf"(?<![\w.+#/-]){escaped}(?![\w.+#-])",
        re.IGNORECASE,
    )


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    """Deduplicate case-insensitively, keeping first-seen spelling and order."""
    seen: set[str] = set()
    kept: list[str] = []
    for value in values:
        text = str(value).strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            kept.append(text)
    return tuple(kept)
