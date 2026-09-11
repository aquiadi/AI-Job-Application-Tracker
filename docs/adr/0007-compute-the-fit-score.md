# 7. Compute the fit score; let the model only narrate it

Date: 2026-09-11
Status: Accepted

## Context

The product's central number is "how well does this person fit this posting". The
cheapest way to produce it is to hand Gemini the resume and the posting and ask for a
score out of 100. That takes one prompt and an afternoon.

It also produces a number with four problems. It is not reproducible: the same inputs
give different answers across calls, and across model versions. It is not explainable:
whatever justification comes back was written after the number and is a plausible
story about it rather than its cause. It cannot be calibrated, because there is no
threshold to move — the only lever is prompt wording. And it cannot be audited: when a
user asks why a role scored 62, there is nothing to point at.

A job seeker deciding where to spend two hours of tailoring effort is making a real
decision on this number. It has to hold up.

## Decision

The score is computed in SQL and Python. No model is in the path.

For each requirement extracted from the posting, take the maximum cosine similarity
against the user's profile items. Map that maximum onto three states by threshold:
covered, partial, or missing. Weight must-haves above nice-to-haves. The score is the
weighted coverage.

Every requirement carries the profile item that produced its maximum, so the interface
shows the evidence next to the judgement rather than the judgement alone.

Gemini's only job here is the gap narrative, and it is given the computed breakdown
and nothing else. It may not introduce a number that is not already in that payload —
the same rule the analytics coaching text follows.

Thresholds and weights are calibrated against hand-labelled profile and posting pairs
in `evals/`, and the Spearman correlation against those labels is reported. They are
not guessed and then defended.

## Alternatives

**Ask the model for the number.** One prompt, no embeddings, no calibration set, and
it would probably correlate reasonably well with human judgement. Rejected for the
four reasons above. The decisive one is calibration: a number nobody can tune against
labelled data cannot be improved, only re-prompted.

**Ask the model per requirement — is this covered, partial, or missing?** A real
improvement on a single global score: the output is structured, it is per-requirement,
and an LLM judge is genuinely better than cosine similarity at spotting that
"designed the idempotent ledger write path" satisfies "distributed transactions".
Rejected as the primary path because it is one model call per requirement on every
posting, which is the highest-volume operation in the system, and because it puts a
nondeterministic step in the number users compare across roles. It is the right tool
for a *second* opinion, and that is where it is used — the tailoring validator runs an
LLM faithfulness judge over generated bullets, where the volume is low and the
question is genuinely semantic.

**A cross-encoder reranker over requirement/item pairs.** Better quality than
bi-encoder cosine, because it sees both texts together. Rejected because it means
hosting a model, which is a Vertex endpoint running whether or not anyone is applying
for jobs — the wrong cost shape for this, and out of proportion to the accuracy it
would buy at this scale.

## Consequences

The score is only as good as the embeddings and the thresholds. A requirement phrased
very differently from how the user described the same work will read as a gap. That is
a real failure mode, it is visible in the calibration eval, and it is why the
interface shows the matched evidence — a user can see the system matched the wrong
thing and fix their profile item.

Nothing can be claimed about the score's quality until the calibration eval runs.
Until then the honest statement is that it is reproducible and explainable, which is
true by construction, and that its agreement with human judgement is unmeasured.

The upside is that the expensive part is already paid for. Requirement and profile
vectors exist for other reasons, so scoring a saved job against a profile is one SQL
query and no model call — which is what makes scoring every saved posting on a profile
change affordable at all.
