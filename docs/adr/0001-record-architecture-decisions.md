# 1. Record architecture decisions

Date: 2026-09-11
Status: Accepted

## Context

This repository is read as much as it is run: it is an audition entry, and the point
is to show the reasoning, not only the result. Most of the interesting decisions here
are choices between two defensible options — vectors in the operational database or in
a dedicated store, the outbox pattern or dual writes, Cloud Tasks or Pub/Sub for
rate-limited work. A reader looking at the code six months from now can see which
option won but not why the other lost, and neither can I.

Commit messages are the wrong place for this. They are ordered by time rather than by
subject, they are not revisited, and a decision that took three commits to implement
has no single message that describes it.

## Decision

Every real decision gets an ADR in `docs/adr/NNNN-title.md`, numbered sequentially,
following `0000-template.md`: context, decision, alternatives, consequences. ADRs are
written before the code that implements them, and are immutable once accepted — a
changed mind is a new ADR that supersedes the old one, not an edit.

"Real decision" means: it constrains future work, it was not the only reasonable
option, or reversing it would cost more than an afternoon. Library version bumps and
naming choices do not qualify.

## Alternatives

**A single `DECISIONS.md`.** Simpler to skim, but it grows into an append-only log that
nobody edits, supersession becomes ambiguous, and there is no stable anchor to link to
from the README or from code.

**Long comments at the top of the relevant module.** These stay close to the code,
which is genuinely better for the *what*. But cross-cutting decisions have no single
module to live in, and the ones that most need explaining — why the fit score is not
generated, why nothing is ever auto-sent — span the whole system.

**Nothing written down.** Honest option for a solo project on a deadline, and it is
what most solo projects do. Rejected because the README has to link a decision trail,
and reconstructing one at the end produces rationalisation rather than record.

## Consequences

There is a per-decision writing cost, paid at the point where the thinking is already
done, so it is small. The README's "Decisions I made" section becomes an index over
files that already exist rather than an essay written at the end.

The failure mode to watch for is ADR inflation — writing one for every choice until
none of them signal anything. The bar above is the guard, and if it slips the fix is to
delete the ones that do not clear it.
