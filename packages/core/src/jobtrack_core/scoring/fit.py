"""The fit score: computed from evidence, never produced by a model.

[ADR 7](../../../../docs/adr/0007-compute-the-fit-score.md) is the decision; this is
the implementation. Each requirement retrieves candidate evidence twice — once by
embedding similarity, once by Postgres full-text search — and the two ranked lists are
fused with Reciprocal Rank Fusion (ADR 12). The best-ranked item becomes that
requirement's evidence, its cosine similarity decides coverage, and the score is the
weighted coverage across all requirements.

Three properties follow from doing it this way, and all three are the point:

**It is reproducible.** The same profile and the same posting give the same number,
every time. A model asked for a score does not.

**It is explainable by construction.** Every point traces to a requirement and the
profile item that answered it, and the interface shows both. There is no post-hoc
justification because there is no hidden step to justify.

**It is calibratable.** The thresholds are constants with a default, and M3's
calibration eval moves them against hand-labelled pairs. Until that runs they are
configuration, and this module says so rather than implying the numbers are earned.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobtrack_core.db.enums import RequirementKind
from jobtrack_core.llm.heuristic.segment import SKILL_VOCABULARY

#: RRF's only parameter, from Cormack et al. Unchanged rather than tuned, because
#: tuning it without an eval is guessing. ADR 12.
RRF_K = 60
#: Candidates each arm retrieves per requirement. Beyond a handful the fused ranking
#: does not change, and every extra row is work in the lateral join.
CANDIDATES_PER_ARM = 10

#: Cosine similarity at or above which a requirement counts as answered. Provisional:
#: not yet calibrated against labelled pairs. See ADR 12.
COVERED_AT = 0.75
#: Between this and COVERED_AT the evidence is related but does not clearly answer.
PARTIAL_AT = 0.55

#: A missing must-have costs more than a missing nice-to-have. This ratio is what
#: makes the score weighted rather than a flat percentage of requirements met.
WEIGHTS: dict[RequirementKind, float] = {RequirementKind.MUST: 1.0, RequirementKind.NICE: 0.3}


class Coverage(enum.StrEnum):
    COVERED = "covered"
    PARTIAL = "partial"
    MISSING = "missing"


#: What each coverage level contributes, as a fraction of its requirement's weight.
CREDIT: dict[Coverage, float] = {
    Coverage.COVERED: 1.0,
    Coverage.PARTIAL: 0.5,
    Coverage.MISSING: 0.0,
}


@dataclass(frozen=True, slots=True)
class RequirementMatch:
    """One requirement, and the strongest evidence found for it."""

    requirement_id: uuid.UUID
    requirement: str
    kind: RequirementKind
    coverage: Coverage
    similarity: float
    #: None when nothing in the profile came close enough to be worth showing.
    evidence_item_id: uuid.UUID | None
    evidence: str | None


@dataclass(frozen=True, slots=True)
class FitScore:
    """The breakdown, and the number derived from it.

    The number is last on purpose. It is a summary of the matches, not an input to
    them, and anything generated from this must read the matches.
    """

    score: int
    matches: tuple[RequirementMatch, ...]
    must_total: int
    must_covered: int
    nice_total: int
    nice_covered: int
    #: The embedding model the comparison ran in. A score computed under the local
    #: hashed backend is a lexical score, and the interface has to be able to say so.
    embedding_model: str | None

    @property
    def is_scorable(self) -> bool:
        return bool(self.matches)


# One statement rather than a loop in Python: the whole comparison stays inside a
# single transaction under the same row-level security policy as everything else, and
# the lateral joins let the HNSW and GIN indexes do the retrieval.
_MATCH_SQL = text(
    """
    WITH req AS (
        SELECT id, text, kind, embedding, display_order
        FROM job_requirements
        WHERE job_id = :job_id AND embedding IS NOT NULL
    ),
    dense AS (
        SELECT r.id AS requirement_id,
               d.item_id,
               d.similarity,
               row_number() OVER (PARTITION BY r.id ORDER BY d.similarity DESC) AS rank
        FROM req r
        CROSS JOIN LATERAL (
            SELECT p.id AS item_id, 1 - (p.embedding <=> r.embedding) AS similarity
            FROM profile_items p
            WHERE p.embedding IS NOT NULL AND p.reviewed
            ORDER BY p.embedding <=> r.embedding
            LIMIT :candidates
        ) d
    ),
    -- `plainto_tsquery` joins every lexeme with AND, so a requirement of five words
    -- only matched an item containing all five. In practice that is never, which left
    -- this arm contributing nothing and made the "hybrid" retrieval dense-only.
    -- Rewriting the operators to OR turns it into what a retrieval query should be:
    -- match any term, rank by how many and how close.
    queries AS (
        SELECT id,
               NULLIF(replace(plainto_tsquery('english', text)::text, '&', '|'), '')::tsquery
                   AS query
        FROM req
    ),
    lexical AS (
        SELECT r.id AS requirement_id,
               l.item_id,
               row_number() OVER (PARTITION BY r.id ORDER BY l.score DESC) AS rank
        FROM req r
        JOIN queries q ON q.id = r.id
        CROSS JOIN LATERAL (
            SELECT p.id AS item_id, ts_rank_cd(p.search, q.query) AS score
            FROM profile_items p
            WHERE p.reviewed AND q.query IS NOT NULL AND p.search @@ q.query
            ORDER BY score DESC
            LIMIT :candidates
        ) l
    ),
    fused AS (
        SELECT requirement_id, item_id, SUM(contribution) AS rrf
        FROM (
            SELECT requirement_id, item_id, 1.0 / (:rrf_k + rank) AS contribution FROM dense
            UNION ALL
            SELECT requirement_id, item_id, 1.0 / (:rrf_k + rank) FROM lexical
        ) contributions
        GROUP BY requirement_id, item_id
    ),
    best AS (
        SELECT DISTINCT ON (requirement_id) requirement_id, item_id, rrf
        FROM fused
        ORDER BY requirement_id, rrf DESC, item_id
    )
    SELECT r.id            AS requirement_id,
           r.text          AS requirement,
           r.kind          AS kind,
           b.item_id       AS evidence_item_id,
           p.text          AS evidence,
           -- The coverage threshold sits on cosine similarity, not on the RRF score:
           -- RRF is rank-based and deliberately has no meaningful scale.
           COALESCE(d.similarity, 0) AS similarity,
           pe.embedding_model        AS embedding_model
    FROM req r
    LEFT JOIN best b ON b.requirement_id = r.id
    LEFT JOIN profile_items p ON p.id = b.item_id
    LEFT JOIN dense d ON d.requirement_id = r.id AND d.item_id = b.item_id
    LEFT JOIN profile_items pe ON pe.id = b.item_id
    ORDER BY r.display_order
    """
)


async def score_job(session: AsyncSession, *, job_id: uuid.UUID) -> FitScore:
    """Score one posting against the caller's reviewed profile items.

    Only reviewed items are eligible. An item imported from a resume is a model's
    reading of that resume until the user confirms it, and scoring against unconfirmed
    text would mean the number rests on something nobody has checked.
    """
    rows = (
        await session.execute(
            _MATCH_SQL,
            {"job_id": str(job_id), "candidates": CANDIDATES_PER_ARM, "rrf_k": RRF_K},
        )
    ).mappings()

    matches: list[RequirementMatch] = []
    embedding_model: str | None = None

    for row in rows:
        similarity = float(row["similarity"])
        shared = shared_technologies(row["requirement"], row["evidence"])
        coverage = classify(similarity, shared_technologies=shared)
        embedding_model = embedding_model or row["embedding_model"]
        # Evidence below the partial threshold is withheld rather than shown. The
        # nearest item to "5 years of Kubernetes" is always *something*, and showing an
        # unrelated bullet next to a gap reads as a match the score did not give.
        keep = coverage is not Coverage.MISSING
        matches.append(
            RequirementMatch(
                requirement_id=row["requirement_id"],
                requirement=row["requirement"],
                kind=RequirementKind(row["kind"]),
                coverage=coverage,
                similarity=round(similarity, 4),
                evidence_item_id=row["evidence_item_id"] if keep else None,
                evidence=row["evidence"] if keep else None,
            )
        )

    return _summarise(matches, embedding_model)


def shared_technologies(requirement: str, evidence: str | None) -> frozenset[str]:
    """Named technologies that appear in both the requirement and its evidence.

    The same vocabulary the skills gap uses, deliberately. Without this the two reads
    of one page could disagree — the skills panel saying PostgreSQL is covered because
    the token is there, the requirements panel saying it is missing because an
    embedding scored the sentence low — and a page that contradicts itself is worse
    than either answer alone.
    """
    if not evidence:
        return frozenset()
    return frozenset(
        skill
        for skill, pattern in SKILL_VOCABULARY
        if pattern.search(requirement) and pattern.search(evidence)
    )


def classify(similarity: float, *, shared_technologies: frozenset[str] = frozenset()) -> Coverage:
    """Turn a similarity into a judgement, with a floor for exact technical overlap.

    A requirement naming Kubernetes, answered by an item naming Kubernetes, is at least
    partially covered whatever the embedding says. That is the argument ADR 12 makes
    for having a lexical arm at all, applied to the judgement rather than only to the
    ranking — and it is what keeps a weak embedding space from reporting a genuine
    match as a gap.

    It is a floor, never a ceiling: shared tokens cannot turn a low similarity into
    COVERED, because sharing a word is not the same as meeting the requirement.
    """
    if similarity >= COVERED_AT:
        return Coverage.COVERED
    if similarity >= PARTIAL_AT or shared_technologies:
        return Coverage.PARTIAL
    return Coverage.MISSING


def _summarise(matches: list[RequirementMatch], embedding_model: str | None) -> FitScore:
    musts = [match for match in matches if match.kind is RequirementKind.MUST]
    nices = [match for match in matches if match.kind is RequirementKind.NICE]

    earned = sum(WEIGHTS[match.kind] * CREDIT[match.coverage] for match in matches)
    possible = sum(WEIGHTS[match.kind] for match in matches)

    # A posting with no embedded requirements scores zero rather than dividing by
    # zero, and `is_scorable` is how the interface tells that apart from a genuine
    # zero — the two mean very different things to someone looking at the page.
    score = (
        int(Decimal(earned / possible * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if possible
        else 0
    )

    return FitScore(
        score=score,
        matches=tuple(matches),
        must_total=len(musts),
        must_covered=sum(1 for match in musts if match.coverage is Coverage.COVERED),
        nice_total=len(nices),
        nice_covered=sum(1 for match in nices if match.coverage is Coverage.COVERED),
        embedding_model=embedding_model,
    )
