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
| 1 | Schema, RLS with cross-tenant test, Identity Platform auth | Not started |
| 2 | JD ingestion: Greenhouse, Lever, pasted text; extraction eval | Not started |
| 3 | Profile import (PDF) and the deterministic fit score | Not started |
| 4 | Grounded tailoring, validator, PDF rendering, faithfulness eval | Not started |
| 5 | Kanban board with stage history | Not started |
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
`EMBEDDING_DIM` is capped at 2000 because pgvector's HNSW index is.

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

Not yet verified: the docker compose stack. The machine it was written on ran out of
disk before the Auth emulator image finished building, so `make up` is unrun. The
Postgres init SQL and the compose health checks are therefore unproven and M1 starts
by running them.

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

Not yet specified.
