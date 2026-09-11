# 5. Ship the audition scope with gcloud scripts, not Terraform

Date: 2026-09-11
Status: Accepted

## Context

This repository is an entry for AIM Code Kitchen, due 30 September 2026, running in a
sandbox Google Cloud project on limited credits. What gets judged is a live Cloud Run
URL, a seeded demo account, a README with an architecture diagram and a cost table,
and a three-minute walkthrough.

The full design in `docs/BRIEF.md` is larger than that: Terraform for every resource,
Cloud Build as the delivery pipeline, six job-board adapters, DOCX rendering, web
push, server-sent events, a ScaNN benchmark, and a full analytics suite. Building all
of it and deploying none of it would be the worst possible outcome, so the scope has
to be cut deliberately rather than by running out of time.

The cut has to fall on infrastructure plumbing rather than on the product's actual
claims. The three things this system says about itself — the score is computed, not
generated; nothing is fabricated; nothing is sent without you — are all in the
application layer, and all three are measurable. Those stay.

## Decision

Seven items, each deployed to Cloud Run before the next one starts:

1. Scaffold, schema, RLS with a cross-tenant test, Identity Platform (Google only)
2. JD ingestion: Greenhouse JSON, Lever JSON, pasted text; 15 labelled JDs
3. Profile import (PDF) and the deterministic fit score
4. Grounded tailoring with the validator; PDF output, no batch mode
5. Kanban board with stage history; polling rather than streaming
6. Nudges: Scheduler to sweep to Cloud Tasks to a Gemini draft, shown in-app
7. Cost instrumentation: cost per JD, per fit score, per tailored resume

Infrastructure is created by `scripts/` with `gcloud`, not Terraform. Deploys run
from a laptop with `scripts/deploy.sh`, not Cloud Build.

Everything cut is listed in `docs/ROADMAP.md` with the reason, so the gap between the
brief and the repository is on the record rather than left as an apparent oversight.

## Alternatives

**Terraform from the start, as the brief specifies.** The right answer for anything
with a second environment or a second engineer: the state file is the record of what
exists, and `terraform destroy` is a reliable way to stop paying. Rejected here
because the sandbox is a single throwaway project, because a good chunk of the
remaining time would go into modules for resources that get created once, and because
the state file becomes its own operational problem when the deadline is close. The
scripts are idempotent and read as a literal description of what was created, which
is most of what Terraform would have provided at this size.

**Cloud Build triggers on push to main.** Genuinely valuable — an eval gate that
blocks a regression is one of the better things in the brief. Rejected for now
because it needs a GitHub connection, a build service account, and a repository the
sandbox project can see, and because a broken trigger blocks deploys at exactly the
wrong moment. `make check` runs the same gates locally and `scripts/deploy.sh`
refuses to deploy when they fail, which preserves the property that matters.

**Keep all six ingestion adapters.** Rejected because Greenhouse and Lever share a
shape (public board JSON) and pasted text covers everything else including LinkedIn
and Workday, which must not be scraped anyway. Ashby and generic HTML would add a
third and fourth fetch path without testing anything new about extraction, which is
where the eval actually points.

**Cut the eval suite instead and ship more features.** Rejected outright. Without
measurement, "the score is computed" and "nothing is fabricated" are marketing. The
eval is the part of this that is hard to fake.

## Consequences

There is no single command that recreates the whole environment, and no state file to
diff against reality. `scripts/` has to stay honest about what it creates, and
teardown is manual. On a sandbox project with one region and one of each resource,
that is an acceptable trade; on anything longer-lived it would not be.

Deploys are unreviewed and come from whatever is on the laptop. `scripts/deploy.sh`
therefore runs `make check` first, refuses to deploy a dirty working tree, and tags
each revision with the commit sha, so a deployed revision can always be traced back
to a commit on `main`.

Cost control becomes a manual habit rather than a property of the infrastructure:
smallest AlloyDB shape, `min-instances 0` on every Cloud Run service, and documented
stop/start commands for the database. The cost table in the README is measured from
`make cost-report`, not estimated.

If the deadline moves or credits allow, the roadmap is ordered so that Terraform and
Cloud Build come first — they are additive, and nothing in the application layer has
to change to adopt them.
