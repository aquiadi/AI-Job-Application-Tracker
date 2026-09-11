# Roadmap

Work that is designed but not built, and why. This exists so the gap between
`BRIEF.md` and the repository is on the record rather than looking like an oversight.

Ordering rationale is in
[ADR 5](adr/0005-audition-scope-and-gcloud-deploys.md).

## Deferred for the audition build

Everything here is additive: adopting any of it requires no change to the application
layer.

### Infrastructure

**Terraform for every resource.** Replaced by idempotent `gcloud` scripts in
`scripts/`. The sandbox is a single throwaway project with one of each resource, so
the state file would cost more than it returns before the deadline. This is the first
thing to add if the timeline moves.

**Cloud Build triggers on push to main**, including the eval gate that fails the build
when skill-F1 or faithfulness drops below the committed baseline. `make check` runs
the same gates locally, and `scripts/deploy.sh` refuses to deploy when they fail, so
the property is preserved without the pipeline. The eval gate against live Vertex is
the piece most worth adding back.

**CMEK.** Documented as an option in `privacy.md`; not configured.

**OpenTelemetry traces to Cloud Trace.** Structured logging with `severity` is in
place, so log-based debugging works. Distributed tracing across api, Pub/Sub and the
worker is not.

### Ingestion

**Ashby and generic career-page adapters.** Greenhouse and Lever share a shape, and
pasted text covers everything else, so a third and fourth fetch path would exercise no
new extraction behaviour. Generic HTML additionally needs main-content extraction,
which is its own problem.

LinkedIn and Workday are not deferred — they are permanently out of scope as fetch
paths. Both prohibit scraping and render client-side. Postings from them are pasted.

### Documents

**DOCX rendering.** PDF only. The ATS-safe single-column HTML template feeding
WeasyPrint is the part that matters; a second renderer is mechanical.

**Batch tailoring via Cloud Tasks fan-out.** One job at a time. The Cloud Tasks path
still gets built for nudges, so the rate-limiting machinery exists; batching is a
matter of enqueuing N tasks instead of one.

### Interface

**Server-sent events.** The board and any in-flight progress poll instead. SSE on
Cloud Run needs care around request timeouts and instance scaling, and polling is
correct at this scale.

**Firebase Cloud Messaging web push.** Nudge drafts appear in the app only. Nothing is
ever sent to an employer either way; this is only about how the user learns a draft is
waiting.

**LinkedIn sign-in.** Google only. LinkedIn OIDC returns name, email and photo and is
not a profile import path, so it adds a second identity provider to configure for no
new capability.

### Measurement

**ScaNN benchmark against pgvector HNSW.** The brief calls for an ADR plus a benchmark
script on a synthetic set before choosing. HNSW is the default and the vector count in
a demo dataset is far below where the difference shows up, so the benchmark would
measure noise. Worth doing when there is enough data for the answer to mean something.

**Analytics beyond a basic funnel.** Funnel conversion per stage is in scope. Median
time-in-stage, callback rate by role family and by source, and Wilson intervals on
every rate are not. The honest-statistics rule still applies to what does ship: any
rate shown carries its n.

## Known rough edges

**No teardown command.** `scripts/` creates infrastructure; removing it is manual.
With Terraform this would be `terraform destroy`.

**Responsive behaviour is not checked automatically.** A real bug was found during M0
— `repeat(auto-fit, minmax(16rem, 1fr))` floored the document at 840px and made every
narrow screen scroll sideways — by loading the page in a narrow iframe and comparing
`scrollWidth` against `clientWidth`. That technique could be a script in
`apps/web/scripts/`, but it needs a running server and a browser, which is a heavier
dependency than the rest of `make check` carries. For now the guard is CSS discipline:
`min-width: 0` on the shell and page grid children, and `minmax(min(16rem, 100%), 1fr)`
for auto-fit tracks.

**Editable installs are fragile in an iCloud-synced directory.** CPython skips any
`.pth` file carrying macOS's `UF_HIDDEN` flag, which iCloud Drive sets on files under a
synced `~/Documents`. `PYTHONPATH` is set explicitly in the Makefile and pytest
configuration so imports do not depend on it, but `uv run` on some other entry point
may still surprise someone. Moving the repository outside the synced directory removes
the problem entirely.
