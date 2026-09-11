# 0013. Let the model only rearrange evidence, and check that it did

Date: 2026-09-11
Status: Accepted

## Context

Tailoring a resume is the feature most likely to do harm. A model asked to "make this
resume fit this job" will produce something fluent, plausible, and containing claims the
person cannot back up in an interview — a technology they have never used, a team size
they never managed, a percentage nobody measured. The user does not necessarily notice,
because it reads like their own work. They find out in the room.

That is not a prompt-engineering problem. "Do not invent anything" is already in every
prompt of this kind, and it reduces the rate without reaching zero. A rule that matters
this much cannot be enforced by asking.

There is a second, quieter failure. Even a model that invents nothing can silently drop
a qualification or merge two roles, and a resume that is shorter than the user expects
without saying why is a resume they will send without noticing what is missing.

## Decision

Generation is constrained at three points, and the middle one is the only one that is
load-bearing.

**Before.** The prompt is built only from reviewed profile items, each given a stable
`[id]` marker. Contact details are not in it — they live on separate columns and are
re-attached at render time. The computed fit breakdown is included so the model knows
which requirements matter, and it is told the numbers in it are the only numbers it may
use.

**During.** Structured output: every generated bullet must carry `source_item_ids`,
naming the profile items it was built from.

**After — the validator.** Every bullet is checked against the items it cites, and a
bullet is rejected if it:

1. cites nothing;
2. cites an id that does not exist, is not the user's, or is not reviewed;
3. contains a number that appears in none of its cited items and in no computed
   breakdown figure;
4. names a technology — matched against the same token vocabulary the skills gap uses —
   that appears in none of its cited items.

A rejected bullet is regenerated once with the specific failure in context. If it fails
again it is dropped and a warning is recorded on the artifact, which the interface
shows. A shorter resume with a visible gap is safer than a complete one with an
invented line.

Rule 3 is the one that catches the most dangerous output. Numbers are what a resume is
believed on, and a model that turns "reduced manual work" into "reduced manual work by
60%" has fabricated the single most checkable claim on the page.

## Alternatives

**Trust the prompt.** Rejected. Instruction-following on this is good and not perfect,
and the cost of the residual failures is borne by the user in an interview.

**Ask a second model to judge faithfulness.** Useful as a measurement and already
planned as the faithfulness eval, but rejected as the enforcement mechanism: it is
non-deterministic, it costs a call per bullet, and a judge that is wrong 5% of the time
is a gate that lets through 5% of fabrications. The deterministic checks are crude and
they do not have a bad day.

**Generate freely and diff against the master resume for review.** Puts the whole
burden on a person reviewing text that reads like their own writing, which is precisely
the condition under which people skim.

## Consequences

Generated bullets are traceable by construction: every one names its sources, they are
stored on the artifact, and the interface can show the original beside the rewrite.

The validator is strict in a way that will sometimes be wrong. A bullet saying "led a
team" when the cited item says "mentored four engineers" passes; one that introduces
"Kubernetes" because the cited item says "container orchestration" is rejected, even
though the user may well know Kubernetes. That asymmetry is deliberate — a dropped true
bullet costs the user a line they can add back by hand, and a kept false one costs them
credibility.

The rejection rate is a measurement worth watching rather than a number to be proud of.
It goes in the eval report, and a rate near zero would most likely mean the checks have
stopped catching anything rather than that generation became perfect.
