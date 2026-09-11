# Specification

What each milestone commits to, written before the code. Sections are added as work
starts on them; a heading with no content means that milestone has not been specified
yet, not that it has no requirements.

Product context: `BRIEF.md`. Decisions: `adr/`. Deferred work: `ROADMAP.md`.

Delivery order is the audition scope in [ADR 5](adr/0005-audition-scope-and-gcloud-deploys.md).
Each item is deployed to Cloud Run before the next one starts.

| # | Milestone | State |
|---|---|---|
| 0 | Scaffold, tooling, design system, sandbox check | Done |
| 1 | Schema, RLS with cross-tenant test, Identity Platform auth | Done, not deployed |
| 2 | JD ingestion: Greenhouse, Lever, pasted text; extraction eval | Built, eval pending |
| 3 | Profile import (PDF) and the deterministic fit score | Built, calibration pending |
| 4 | Grounded tailoring, validator, PDF rendering, faithfulness eval | Not started |
| 5 | Kanban board with stage history | Done |
| 6 | Nudges: sweep, Cloud Tasks, drafts | Not started |
| 7 | Cost instrumentation and `make cost-report` | Not started |

---

## M0 — Scaffold

### Goal

A repository where `make check` is meaningful, the local stack costs nothing to run,
and the one question that could invalidate the whole plan — can this sandbox project
actually run AlloyDB and Vertex AI — is answerable in under a minute.

### Delivered

**Sandbox verification.** `scripts/sandbox_check.sh` enables and then *exercises*
nine APIs: Vertex AI, AlloyDB, Cloud Run, Pub/Sub, Cloud Tasks, Cloud Scheduler,
Identity Platform, Secret Manager, Cloud Storage. Enablement alone is not evidence, so
each gets one real call: `countTokens` against both generative models at
`VERTEX_LOCATION`, a one-token `predict` against the embedding model at
`EMBEDDING_REGION`, a cluster list for AlloyDB, a list call for the rest, and the
Identity Toolkit admin config endpoint for Identity Platform. Exit 2 if AlloyDB or
Vertex AI is unusable, with the likely causes in order; exit 1 for non-blocking
failures; exit 3 for a missing prerequisite.

**Workspace.** One uv workspace, Python 3.12, four members with a one-way dependency
graph ([ADR 3](adr/0003-uv-workspace-layout.md)). Ruff for lint and format with
relative imports banned, `mypy --strict` over all four, pytest with warnings as
errors.

**Configuration.** `jobtrack_core.config.Settings`, read from the environment and a
`.env` file. Three separate Google Cloud locations, not one
([ADR 2](adr/0002-region-and-vertex-endpoints.md)). `ENVIRONMENT=cloud` fails at
startup listing every missing variable at once rather than one per deploy.
`EMBEDDING_DIM` is capped at 2000, which is pgvector's index limit for the `vector`
type.

**Logging.** Structured JSON carrying `severity` and `message` so Cloud Logging reads
it as typed rather than as text at INFO. A processor drops any log call carrying a
field from a banned list — `resume_text`, `jd_text`, `email`, `bullets` and the rest —
and replaces it with an error naming the offending keys, so a PII leak surfaces in
review instead of in a log bucket.

**Local stack.** `docker compose` runs `pgvector/pgvector:pg16`, the Pub/Sub
emulator, and the Firebase Auth emulator (built on the official Node image with
`firebase-tools` pinned, rather than a community image). Postgres initialises with
the same privilege split the deployed database will have: `jobtrack_owner` owns the
schema and runs migrations, `jobtrack_app` owns nothing and holds only DML grants, so
row-level security is enforced against the application for the same reason it will be
in AlloyDB. A separate `jobtrack_test` database keeps a test run from destroying
development data.

**Web.** Next.js 16 App Router, TypeScript strict with `noUncheckedIndexedAccess` and
`exactOptionalPropertyTypes`, standalone output for Cloud Run. Design tokens in one
file, two self-hosted faces, and an evidence scale built from ink density rather than
hue ([ADR 6](adr/0006-design-tokens-in-plain-css.md)). WCAG AA contrast is enforced by
a script over the token file, not asserted.

**Services.** `api` and `worker` are separate FastAPI apps, each with a liveness
endpoint that deliberately touches no dependency. The worker publishes no OpenAPI
document, because its endpoints are callable only by Pub/Sub and Cloud Tasks with a
service-account token.

### Not in M0

No database schema, no migrations, no authentication, no Gemini calls, no Dockerfiles,
no deploy. Those are M1 and later. The Makefile carries only targets whose
implementation exists.

### Verification

`make check` runs, in order: ruff lint, ruff format check, `mypy --strict`, pytest,
then the web app's typecheck, lint, format check and contrast check. It is green at
this commit with 40 Python tests and 44 checked contrast pairings. `npm run build`
produces a standalone Next.js server.

`make up` brings all three containers to healthy. Verified against the running
stack: both databases exist and are owned by `jobtrack_owner`; `vector`, `uuid-ossp`
and `pg_trgm` are installed; neither role is a superuser; and `jobtrack_app` is
refused when it tries to create a table in `public`, which is the privilege split
row-level security depends on. The Pub/Sub emulator accepts a topic create and lists
it back, and the Auth emulator reports ready.

Postgres binds host port 5433 rather than 5432, so the stack coexists with another
Postgres on the same machine. `DB_PORT` moves both the bind and the client.

---

## Makefile targets

The full intended set. Each appears in the Makefile in the commit that makes it work.

| Target | Milestone | Purpose |
|---|---|---|
| `help` `install` `up` `down` `clean` | M0 | Setup and the local stack |
| `dev` | M0 | api on 8080, worker on 8081, web on 3000 |
| `check` `lint` `fmt` `typecheck` `test` `web-check` | M0 | Quality gates |
| `sandbox-check` | M0 | Verify the Google Cloud sandbox |
| `migrate` `migration` | M1 | Alembic |
| `test-integration` | M1 | Tests needing the local stack, including cross-tenant RLS |
| `deploy` | M1 | Build images, deploy every service to Cloud Run |
| `db-stop` `db-start` | M1 | Stop paying for AlloyDB vCPUs between sessions |
| `eval` `label` | M2 | Run the eval suite; label a dataset |
| `seed` | M5 | Demo user with synthetic data |
| `cost-report` | M7 | Measured cost per operation, for the README |

---

## M1 — Data and authentication

### Goal

A database where one user cannot read another's rows even if the application asks it
to, and an API that knows who is calling.

### Delivered

**Schema.** Twelve tables. Every tenant table carries `user_id`; `users` is scoped by
its own `id`; `jd_extraction_cache` is global by design and holds only public posting
content. Every vector column carries `embedding_model`, `embedding_dim` and
`embedding_task_type`, because the three together identify the space and a comparison
across spaces returns a number rather than an error.

**Row-level security.** `ENABLE` plus `FORCE` on all eleven tenant tables, with a
policy per table comparing against
`NULLIF(current_setting('app.user_id', true), '')::uuid`. The `NULLIF` is not
decoration: an empty setting raises on the cast, which would turn a missing tenant
context into a 500 rather than an empty result. Absent context matches no row.

Three roles, and the separation is the point. `jobtrack_owner` owns the schema and runs
Alembic. `jobtrack_app` owns nothing and holds only DML grants, because Postgres does
not enforce a policy against a table's owner unless the table also sets `FORCE`, and
relying on `FORCE` alone leaves no margin. `stage_events` is append-only by grant —
`INSERT` and `SELECT`, nothing else — because every funnel number derives from it.

**The tenant is applied with `set_config(..., true)`, not `SET LOCAL`.** `SET LOCAL`
takes a literal rather than a bind parameter, so using it would mean formatting a user
id into SQL text. It is applied inside the transaction on every request, because a
pooled connection outlives the request and a setting applied at checkout would hand one
user's context to the next.

**Authentication.** Identity Platform ID tokens, verified against Google's signing
certificates with a TTL cache so there is no HTTP round trip per request. The issuer is
checked explicitly: `google.auth` verifies signature, expiry and audience but not
`iss`, and a token from a different Firebase project carries a genuine Google
signature. Emulator tokens are unsigned, so that path is only reachable when
`FIREBASE_AUTH_EMULATOR_HOST` is set, and `Settings` refuses to start if that happens
alongside `ENVIRONMENT=cloud`.

**The user id is derived, not looked up.** `uuid5(namespace, subject)`. A tenant-scoped
query needs the internal id before it can set the RLS context, and finding it by
subject would be a query against a tenant-scoped table — normally resolved with a
privileged lookup that bypasses RLS. Deriving it removes the problem: the tenant
context is computable from the token alone, and no code path reads user rows without a
tenant set. The trade is that the id is a pure function of the subject, so it is
computable by anyone holding it. Ids are not secrets here; the policies are what
protect a row.

**`GET /me`** returns the caller's account and provisions it on first sign-in with
`ON CONFLICT DO NOTHING`, because a browser loading the shell and its first data call
together produces two of these at once on a new account.

### Not in M1

No deploy: that needs a project id and `gcloud` credentials. No outbox relay — the
table and its policy exist, the relay and its dedicated role arrive with M2 when there
are events to publish. No `DELETE /me` or `GET /me/export` yet, though `ON DELETE
CASCADE` from `users` is what will make deletion complete rather than a sweep that
misses a table added later.

### Verification

126 unit tests and 20 integration tests. The cross-tenant test asserts four separate
things — reads, default-deny, writes, and policy coverage — because "RLS is on" is four
claims and a read-only test passes happily while writes are unprotected.

It was checked against a deliberate regression: disabling RLS on one table fails 7 of
the 12 RLS tests. A test that cannot fail is not evidence.

---

## M2 — Ingestion

### Goal

A posting a person actually has — a link they copied, or text they selected — becomes a
row with its requirements separated, classified and embedded, without anyone waiting on
a model inside a request.

### Delivered

**Two stages with a boundary.** Adapters turn a board's payload into
`CanonicalPosting`; extraction sees only `CanonicalPosting`. Adding an ATS never
touches a prompt. [ADR 11](adr/0011-canonical-posting-schema-and-ingestion.md).

**Greenhouse and Lever, verified against their live APIs on 2026-09-11.** Greenhouse
returns the description HTML-escaped inside `content`; Lever splits it across
`description`, a `lists` array of titled sections, and `additional`. The section titles
are kept, because "Required Qualifications" above a block of bullets is the signal that
says those bullets are must-haves.

**Pasted text, which is the source that always works.** Every ATS adapter is a bet that
a vendor keeps an endpoint stable. This one is not, and it is why the product has no
hard dependency on any board.

**A global extraction cache.** Keyed by `sha256` over normalised body, title and
company, plus `CANONICAL_SCHEMA_VERSION`. Two users who save the same posting pay for
one extraction between them. It holds only posting content and has no `user_id`.

**The outbox relay, under its own database role.** `jobtrack_relay` holds `SELECT` and
`UPDATE` on `outbox` and no grant on any other table. Reading across tenants is the one
thing the tenant policy forbids, and granting it to the application role would widen
the application's reach by the same amount. Migration 0002.

**Three model backends behind one protocol.**
[ADR 10](adr/0010-llm-boundary-and-offline-operation.md). `vertex` is the real one;
`cassette` replays recordings for tests; `heuristic` is rule-based, runs in-process and
needs no credentials. `ENVIRONMENT=cloud` with a non-Vertex backend fails at startup.

### Not delivered

The extraction eval over 15 labelled postings. It needs Vertex credentials to produce
the number that matters — Gemini's score against the heuristic baseline — and running
only the baseline would report a floor as though it were a result.

---

## M3 — Profile and the fit score

### Goal

A score that a person can argue with: reproducible, traceable to a specific line of
their own history, and never written by a model.

### Delivered

**The profile, in two halves.** Contact details on their own columns, never in prompt
context, re-attached at render time. Evidence as `profile_items`, one row per bullet,
each embedded on its own — a whole resume as one vector answers "is this person roughly
like this posting", and a bullet answers the question the score actually asks.

**Resume import, which is trusted by nobody.** A PDF's text layer becomes unreviewed
items. Nothing unreviewed is counted by the score or citable by generated content. A
scan is refused with a message saying why rather than importing nothing.

**Hybrid retrieval fused by RRF.**
[ADR 12](adr/0012-hybrid-retrieval-with-rrf.md). Dense retrieval over pgvector and
lexical retrieval over a generated `tsvector`, each ranked, fused with `k=60`. Dense
alone scores "Kubernetes" against "Docker and Terraform"; lexical alone scores every
honest paraphrase as a gap.

**One SQL statement.** Two lateral retrievals per requirement, fused, best-ranked item
per requirement, coverage from that item's cosine similarity. Inside one transaction
under the same RLS policy as everything else.

### Not delivered

Threshold calibration. `COVERED_AT` and `PARTIAL_AT` are constants with a default and
are not yet moved against labelled pairs, which needs real embeddings. The interface
shows the matched evidence beside every judgement so the number is checkable by eye in
the meantime.

---

## M5 — The pipeline

### Goal

A board that is a view over history rather than a place state is kept.

### Delivered

**Transitions are refused in the domain layer.** The router calls
`domain.stages.transition` and turns its refusal into a 409. The list of legal next
moves the interface offers is derived from the same function, so the two cannot drift.

**Every move appends a `stage_events` row**, including the one that creates the
application. Without that first row the history would begin at the first move and the
time an application spent in `Saved` — where most of them die — would be unmeasurable.

**Terminal stages are separated from the pipeline** in the response, because they are
outcomes rather than steps.
