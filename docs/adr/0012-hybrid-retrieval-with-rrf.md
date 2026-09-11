# 0012. Match requirements to evidence with hybrid retrieval, fused by RRF

Date: 2026-09-11
Status: Accepted

## Context

[ADR 7](0007-compute-the-fit-score.md) settled that the fit score is computed rather
than asked for, and that the unit of comparison is one requirement against one profile
item. It left open how a requirement finds its best evidence.

Dense retrieval — cosine similarity over embeddings — is the obvious answer and it has
a specific, well-documented weakness. Embeddings are good at paraphrase and poor at
rare exact tokens. "Kubernetes", "CPA", "Series 7", "FedRAMP", "PostgreSQL 16" are
precisely the terms a posting uses to mean something non-negotiable, and precisely the
terms a dense model will happily match against a nearby concept. A candidate who has
never touched Kubernetes can score well against "Operating services on Kubernetes"
because their bullet mentions Docker and Terraform.

That failure is quiet, and it is the expensive kind: it inflates the score, which is
the number the whole product is built to make trustworthy.

Lexical retrieval has the mirror weakness. Postgres full-text search over
`to_tsvector('english', ...)` matches "Kubernetes" exactly and scores "built container
orchestration at scale" at zero.

There is also a constraint the local backend imposes. `HeuristicLlmClient` produces
hashed lexical vectors, not semantic ones (ADR 10). Under it, dense retrieval is
lexical retrieval with extra steps. A scoring path that depends on dense similarity
alone would therefore be untestable offline in any meaningful sense.

## Decision

Each requirement retrieves candidate evidence twice and the two ranked lists are fused
with Reciprocal Rank Fusion.

**Dense.** Cosine distance against `profile_items.embedding` using pgvector's `<=>`
operator, with an HNSW index on `vector_cosine_ops`.

**Lexical.** `ts_rank_cd` against a `tsvector` generated column on `profile_items`,
with a GIN index. Generated and stored rather than computed per query, because the
column is read on every scoring pass and written once per profile edit.

**Fusion.** RRF with k=60: an item's fused score is the sum over both lists of
`1 / (k + rank)`. Rank-based rather than score-based, which is the property that
matters here — a cosine similarity and a `ts_rank_cd` value have no common scale, and
normalising them against each other would require calibration constants that drift with
every model and corpus change. Ranks need none. k=60 is the value from the original
Cormack et al. formulation and is used unchanged; tuning it is on the list in
`ROADMAP.md`, not something to guess at now.

The fused ranking chooses *which* evidence answers a requirement. The coverage
judgement — covered, partial, missing — still comes from the dense cosine similarity of
the chosen item, because a threshold has to sit on a scale with meaning, and RRF's
output deliberately has none.

## Alternatives

**Dense only.** Rejected on the exact-token failure above. It is also the option that
would make the offline path a lie.

**Lexical only.** Rejected: it scores every honest paraphrase as a gap, which trains
the user to write their resume in the posting's words. That is keyword stuffing with
extra steps, and the product exists to do the opposite.

**Weighted sum of normalised scores.** Rejected. It needs a normalisation and a weight,
and both are constants that would have to be re-derived whenever the embedding model or
the corpus changes. RRF has one parameter with a published default.

**A cross-encoder reranker.** Genuinely the better answer for quality, and it is a
second model call per requirement per scoring pass. At a few dozen requirements per
posting that is a real cost on a limited-credit sandbox, and the value it adds over RRF
is unmeasured here. Deferred to `ROADMAP.md`.

## Consequences

Exact technical terms are matched by the lexical arm, and paraphrase by the dense arm,
without either being able to dominate through score magnitude.

Scoring becomes one SQL statement with two CTEs rather than a Python loop over
similarities, which keeps it inside one transaction under the same RLS policy as
everything else.

It costs a generated column and a GIN index on `profile_items`, and a second index to
maintain on every profile edit. Profile edits are rare and scoring is frequent, so the
trade is in the right direction.

The honest gap: nothing here is yet *measured*. Whether the fused ranking beats dense
alone on this corpus is exactly what M3's calibration eval is for, and the thresholds
that turn a similarity into covered/partial/missing are not believable until it runs
against real embeddings. Until then they are configuration with a default, and the
interface shows the matched evidence beside every judgement so the user can see what
the number was built from.

---

## Amendment, 2026-09-11: two corrections found by looking at the page

**The lexical arm was contributing nothing.** The query was built with
`plainto_tsquery`, which joins every lexeme with `AND`. A five-word requirement
therefore only matched a profile item containing all five words, which in practice is
never — `@@` was false, the arm returned no rows, and the "hybrid" retrieval had been
dense-only since it was written. The operators are now rewritten to `OR`, which is what
a retrieval query should be: match any term, rank by how many and how close. Found by
running the two queries side by side against a real requirement, not by a failing test;
the tests asserted the fused result, and a fusion of one arm and an empty arm looks
exactly like a working fusion.

**Coverage now has a floor for exact technical overlap.** Where a requirement and its
chosen evidence share a named technology from the same vocabulary the skills gap uses,
coverage is at least `partial`, whatever the embedding scored. The symptom was a page
contradicting itself: the skills panel reporting PostgreSQL as covered because the token
was present, and the requirements panel calling the same thing missing because the
lexical embedding scored the sentence low.

It is a floor and never a ceiling — shared tokens cannot produce `covered`, because
sharing a word is not meeting the requirement. It is also the argument this ADR already
makes for having a lexical arm, applied to the judgement rather than only to the
ranking.
