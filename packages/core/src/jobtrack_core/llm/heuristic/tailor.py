"""Rule-based tailoring. The offline counterpart to the tailoring prompt.

This one is the most honest about its own limits. It cannot rewrite, because rewriting
is exactly the thing a rule cannot do — so it does not try. It *selects* and *orders*:
it keeps the evidence items that answer this posting's requirements, groups them by
employer in the order the fit breakdown found them useful, and returns each one
unchanged, citing itself.

That is a genuinely useful floor and a genuinely different product. The output is a
resume of the user's own lines, reordered for the role, which is the half of tailoring
that carries most of the value and none of the risk. What the model adds on top —
phrasing a bullet in the posting's vocabulary — is what the eval measures.

Because every bullet is its own source, this output passes the grounding validator by
construction. That is a property worth having: it means the validator can be exercised
end to end offline without pretending a rule-based writer is a model.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict

from jobtrack_core.db.enums import ProfileItemKind
from jobtrack_core.generate.schemas import (
    MIN_BULLET_CHARS,
    TailoredBullet,
    TailoredResume,
    TailoredSection,
)

#: Bullets per tailored resume. Enough for three roles at four bullets each.
MAX_SELECTED = 14
#: Heading the collected skill items are gathered under.
SKILLS_HEADING = "Skills"


def tailor(
    *,
    items: list[tuple[uuid.UUID, str, str | None, str | None, ProfileItemKind]],
    relevant_item_ids: list[uuid.UUID],
) -> TailoredResume:
    """Order evidence by relevance to this posting. Returns each item unchanged.

    Ordering is the tailoring here, not selection. An earlier version kept only the
    items the fit breakdown had matched to a requirement, and on a posting where the
    embedding matched one requirement that produced a one-bullet resume — technically
    relevant and useless to send. A resume has to be complete enough to be a resume.

    So matched items lead, in the order the breakdown found them useful, and the rest
    follow in the user's own display order. What the tailoring does is decide what a
    reader sees first.

    Args:
        items: (id, text, organisation, role, kind) for every reviewed profile item.
        relevant_item_ids: item ids the breakdown matched to a requirement, most
            relevant first.
    """
    by_id = {
        item_id: (text, organisation, role, kind)
        for item_id, text, organisation, role, kind in items
    }

    ordered: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for item_id in [*relevant_item_ids, *by_id]:
        if item_id in by_id and item_id not in seen:
            seen.add(item_id)
            ordered.append(item_id)

    # Skills are collected into one line rather than becoming bullets of their own. A
    # resume lists them; and individually they are one or two words, which is shorter
    # than a bullet is allowed to be — the schema minimum exists so a model cannot
    # answer with fragments, and a skills row is not a fragment, it is a different
    # kind of content.
    skills = [item_id for item_id in ordered if by_id[item_id][3] is ProfileItemKind.SKILL]
    prose = [item_id for item_id in ordered if by_id[item_id][3] is not ProfileItemKind.SKILL]

    grouped: OrderedDict[tuple[str, str | None, str | None], list[TailoredBullet]] = OrderedDict()
    for item_id in prose[:MAX_SELECTED]:
        text, organisation, role, _ = by_id[item_id]
        if len(text.strip()) < MIN_BULLET_CHARS:
            # Defensive: a prose item this short is a parsing artefact, and letting it
            # through would fail schema validation deep inside generation rather than
            # here, where the reason is obvious.
            continue
        heading = organisation or role or "Experience"
        grouped.setdefault((heading, organisation, role), []).append(
            TailoredBullet(text=text, source_item_ids=[item_id])
        )

    sections = [
        TailoredSection(heading=heading, organisation=organisation, role=role, bullets=bullets)
        for (heading, organisation, role), bullets in grouped.items()
    ]

    if skills:
        joined = ", ".join(by_id[item_id][0] for item_id in skills)
        if len(joined) >= MIN_BULLET_CHARS:
            # Cites every skill row it was built from, so the validator can check it
            # the same way it checks any other bullet.
            sections.append(
                TailoredSection(
                    heading=SKILLS_HEADING,
                    bullets=[TailoredBullet(text=joined, source_item_ids=skills)],
                )
            )

    return TailoredResume(
        # No summary: writing one would mean composing a sentence from several items,
        # which is the part this cannot do without inventing the connective tissue.
        summary=None,
        sections=sections,
    )
