# 3. One uv workspace, four packages, one-way dependencies

Date: 2026-09-11
Status: Accepted

## Context

Three Python processes ship: `api`, `worker`, and a migration job. They share a lot —
the Pydantic schemas, the database session with its row-level-security hook, the
Gemini client, the domain state machine — and almost none of that shared code should
know which process it is running in.

The shape of the repository decides whether that stays true. If everything lives in
one package, nothing stops a router importing a Pub/Sub handler, and the boundary
between "domain rule" and "HTTP concern" erodes in about a week. If the shared code
is a separately versioned library, every change needs a release before the services
can use it, which is a real tax on a solo project against a deadline.

## Decision

A single uv workspace with four members and a one-way dependency graph:

```
packages/core     depends on nothing in this repo
services/api      depends on core
services/worker   depends on core
evals             depends on core
apps/web          depends only on the api's OpenAPI document
```

`packages/core` holds the schemas, the database session and RLS helpers, the LLM
client, the domain layer, and the interfaces for every Google Cloud service. The
services hold routers, handlers, and the adapters that implement those interfaces.
Nothing in `core` imports FastAPI.

Ruff enforces the parts a reviewer would otherwise have to police: relative imports
are banned outright (`ban-relative-imports = "all"`), so every import states which
package it crosses into and an accidental sideways dependency is visible in the diff.
`mypy --strict` runs over all four.

## Alternatives

**One flat package with subpackages.** Fewer files, no workspace configuration, and
`make check` is a single invocation either way. Rejected because the import boundary
would be a naming convention rather than a fact, and the thing most worth protecting
here — that the domain layer has no idea Google Cloud exists — is exactly the kind of
boundary that a convention loses.

**Separate repositories per service with `core` published to Artifact Registry.**
The strongest boundary, and the right answer for several teams. Rejected for one
engineer on a deadline: every cross-cutting change becomes publish, wait, bump, and
the eval suite would need a released version of `core` to run against.

**Poetry or plain pip with a requirements file.** uv resolves this workspace in
seconds, produces a single lockfile covering every member, and is the same tool the
container build uses. No reason to reach past it.

## Consequences

`uv sync --all-packages` installs every member editable, and a plain `uv run` syncs
only the root project — which will happily uninstall those members. The root's dev
dependency group therefore lists all four members explicitly, so any `uv run` gets a
complete environment. That is a uv-specific wrinkle worth knowing about before it
shows up as a confusing `ModuleNotFoundError`.

Editable installs are also fragile on a machine where the repository sits in an
iCloud-synced directory: they work through a `.pth` file in `site-packages`, and
CPython 3.11+ skips any `.pth` carrying macOS's `UF_HIDDEN` flag, which iCloud sets.
`PYTHONPATH` is therefore set explicitly in the Makefile and `pythonpath` in the
pytest configuration, so imports never depend on that mechanism. Containers install
the packages normally and are unaffected.

Test files across the services share names (`test_health.py`), so pytest runs with
`--import-mode=importlib` rather than requiring `__init__.py` files through the test
tree.
