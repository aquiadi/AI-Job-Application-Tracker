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

from jobtrack_core.generate.schemas import TailoredBullet, TailoredResume, TailoredSection

#: Bullets per tailored resume. Enough for three roles at four bullets each.
MAX_SELECTED = 14


def tailor(
    *,
    items: list[tuple[uuid.UUID, str, str | None, str | None]],
    relevant_item_ids: list[uuid.UUID],
) -> TailoredResume:
    """Select and group evidence. Returns each item unchanged, citing itself.

    Args:
        items: (id, text, organisation, role) for every reviewed profile item.
        relevant_item_ids: item ids the fit breakdown matched to a requirement, most
            relevant first. Items outside this list are dropped rather than appended —
            a tailored resume that ends with everything else is not tailored.
    """
    by_id = {item_id: (text, organisation, role) for item_id, text, organisation, role in items}
    ordered = [item_id for item_id in relevant_item_ids if item_id in by_id][:MAX_SELECTED]

    grouped: OrderedDict[tuple[str, str | None, str | None], list[TailoredBullet]] = OrderedDict()
    for item_id in ordered:
        text, organisation, role = by_id[item_id]
        heading = organisation or role or "Experience"
        grouped.setdefault((heading, organisation, role), []).append(
            TailoredBullet(text=text, source_item_ids=[item_id])
        )

    return TailoredResume(
        # No summary: writing one would mean composing a sentence from several items,
        # which is the part this cannot do without inventing the connective tissue.
        summary=None,
        sections=[
            TailoredSection(heading=heading, organisation=organisation, role=role, bullets=bullets)
            for (heading, organisation, role), bullets in grouped.items()
        ],
    )
