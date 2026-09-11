# 0011. Normalise every posting into one versioned schema before extraction

Date: 2026-09-11
Status: Accepted

## Context

A job posting arrives in one of several shapes. Greenhouse and Lever both publish JSON
job board APIs, and their payloads agree on almost nothing: Greenhouse returns
`content` as HTML-escaped markup with `location.name` and a `metadata` array; Lever
returns `description` plus a `lists` array of `{text, content}` sections, with
`categories.location`. A pasted posting has no structure at all.

Extraction quality depends on what the model is shown. Handing Gemini raw Greenhouse
HTML wastes input tokens on markup and invites the model to treat navigation text as
requirements. Handing it a Lever payload straight means the prompt has to understand
Lever, and again for every adapter added later.

There is also a caching constraint that predates this decision. `jd_extraction_cache`
is global and keyed by content hash, which only pays for itself if two users saving the
same posting produce the same key. A hash over the raw payload fails that: Greenhouse
embeds a request-scoped `updated_at`, and a user who pastes the same text with
different whitespace gets a different hash.

## Decision

Ingestion is two stages with a defined boundary between them.

**Stage one, adapters.** Each source has an adapter that turns its payload into
`CanonicalPosting`: a Pydantic model holding `title`, `company`, `location`,
`source_url`, `posted_at`, and `body` as plain text with structure preserved as
markdown-style bullets. Adapters are pure functions over bytes. They perform no
network calls of their own and no model calls at all.

**Stage two, extraction.** Extraction sees only `CanonicalPosting`. The prompt knows
nothing about Greenhouse or Lever, so adding an adapter never changes a prompt, and the
extraction eval's dataset is source-independent.

The content hash is `sha256` over the *normalised body* — whitespace collapsed, case
preserved, markup removed — concatenated with the title and company. Two users saving
the same posting from different URLs hit the same cache entry.

`CANONICAL_SCHEMA_VERSION` is an integer constant compiled into the cache key and
stored on every `jobs` row. Bumping it invalidates cached extractions by construction
rather than by a manual purge.

URL routing is by host pattern: `boards.greenhouse.io` and `job-boards.greenhouse.io`
to the Greenhouse adapter, `jobs.lever.co` to Lever, anything else rejected with a
message naming the supported sources. Pasted text bypasses routing.

## Alternatives

**One prompt per source.** Rejected. It multiplies the prompt surface by the number of
adapters, and every prompt is a thing to version, eval and regress.

**Extract straight from HTML with no canonical stage.** Rejected on token cost and on
the cache-key problem: markup varies between requests for the same posting.

**Hash the extracted output rather than the input.** Circular — the extraction is what
the cache exists to avoid paying for twice.

**Store only the canonical form and discard the raw payload.** Rejected. Bumping
`CANONICAL_SCHEMA_VERSION` has to be able to re-derive the canonical form, and an
adapter bug is only diagnosable against the bytes that triggered it. Raw payloads go to
Cloud Storage with a lifecycle rule, and `raw_gcs_uri` on `jobs` points at them.

## Consequences

Adding an ATS is one file, one test and one routing entry. Ashby, Workday, LinkedIn and
generic HTML are already enumerated in `SourceAts` and deferred in `ROADMAP.md`; none
of them require touching extraction.

The canonical stage is a lossy step and that loss is deliberate. Salary tables rendered
as HTML `<table>` markup flatten to text and sometimes extract worse than they would
from markup. Measured, not assumed: the extraction eval reports per-field accuracy, and
salary is one of the fields.

The cache is only as safe as the promise that nothing user-derived reaches it. That is
enforced structurally — `CanonicalPosting` has no field that can hold user data, and
the cache write takes a `CanonicalPosting`, never a `Job` row.
