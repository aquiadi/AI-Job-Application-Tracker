# Extraction eval

Run 2026-09-11 11:30 UTC over 5 labelled postings, 0 of them human-reviewed.

**Unreviewed labels are included in these numbers.** A model-drafted label scored against a model is not a measurement, so treat anything here as provisional until every label is checked.

| backend | postings | recall | precision | F1 | must/nice | fields | hallucinated |
|---|---|---|---|---|---|---|---|
| heuristic | 5 | 0.83 | 1.00 | 0.90 | 1.00 | 0.44 | 0.00 |
| vertex | 5 | did not run | | | | | |

**recall / precision** — labelled requirements found, and found requirements that were real. Matched by token overlap, because a correct extraction may trim a bullet differently than a labeller did.

**must/nice** — of the requirements that were found, how many were classified correctly. The fit score weights must-haves above nice-to-haves, so this decides whether that weighting means anything.

**hallucinated** — share of fields the posting leaves unstated that the backend filled in anyway. Lower is better, and this is the number that decides whether output can be shown without a warning attached.

Only one backend ran. `heuristic` is a rule-based floor, not a result: it exists so that a model's score has something to be measured against. Set `LLM_BACKEND=vertex` with credentials to get the comparison.

### vertex failures

- backend-payments: LlmError
- data-engineer-seattle: LlmError
- frontend-with-salary: LlmError
- ml-engineer-no-salary: LlmError
- sre-prose-requirements: LlmError
