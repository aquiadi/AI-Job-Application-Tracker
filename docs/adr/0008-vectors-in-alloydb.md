# 8. Keep the vectors in AlloyDB rather than a separate vector store

Date: 2026-09-11
Status: Accepted

## Context

Two kinds of row need embeddings: `job_requirements`, one per requirement in a
posting, and `profile_items`, one per bullet, project or skill in the user's history.
Scoring a posting means comparing one set against the other for a single user.

Both are already relational rows. A requirement belongs to a job, a profile item
belongs to a profile, both belong to a user, and both are covered by the row-level
security policy that keeps one user's data away from another's. The vector is an
attribute of a row that has to exist anyway.

The default modern answer is still a dedicated vector store, so it is worth being
explicit about why that is wrong here.

## Decision

Vectors live in AlloyDB, in `vector(768)` columns on the same rows as everything else,
indexed with pgvector HNSW and cosine distance.

The fit score is then one query: join the user's profile items against the
requirements of one job, take the per-requirement maximum similarity, return the
matched item id alongside it. One transaction, one consistency model, one security
policy.

Every vector row carries `embedding_model`, `embedding_dim` and
`embedding_task_type`, so a model change is a re-embed job rather than a schema
migration, and rows from two different vector spaces can never be silently compared.

## Alternatives

**Vertex AI Vector Search.** The managed option inside the Google Cloud constraint,
and the right answer at a scale this will not reach: tens of millions of vectors,
sub-100ms ANN, and no index tuning. Rejected on three counts. Tenant isolation would
become a second, different mechanism — namespace filters rather than the RLS policy
protecting every other table — and two isolation models is how a leak happens.
Consistency becomes eventual, so a newly extracted posting would be scoreable before
its vectors land, which the UI would have to explain. And it is a second place
resume-derived data lives, which means a second thing `DELETE /me` has to reach and a
second thing to get right in `privacy.md`.

**Cloud SQL for PostgreSQL with pgvector.** Supports the same extension, costs less at
the minimum, and would work. This is the closest call. Chosen against because AlloyDB
keeps two doors open that matter if this grows past a demo: the ScaNN index for when
the vector count outgrows HNSW's memory, and the columnar engine for analytics over
`stage_events`, which is the other query shape this product has. The cost of that
optionality is a higher floor, handled by running plain Postgres locally and stopping
the instance between sessions.

**A separate store outside Google Cloud** — Pinecone, Qdrant, pgvector on some other
host. Out of scope by constraint, and the isolation and deletion arguments above apply
with more force across a trust boundary.

## Consequences

The cost floor is AlloyDB's, not Cloud SQL's, and AlloyDB has no scale-to-zero. That
is the price of this decision and it is paid monthly. The mitigations are the smallest
instance shape, documented stop and start commands, and local Postgres for
development — none of which change the fact that an idle deployed instance still
costs money.

pgvector's HNSW index is mine to tune. Nobody manages `m`, `ef_construction` or
`ef_search` for me, and at some vector count the index stops fitting comfortably in
memory on a small instance. The ScaNN comparison is deliberately deferred until there
is enough data for the answer to mean something; running it on a synthetic set now
would measure the generator, not the index.

The thing I get in exchange is that there is exactly one place user data lives. One
backup, one deletion path, one row-level security policy, and no sync job that can be
behind.
