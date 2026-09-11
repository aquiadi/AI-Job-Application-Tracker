# Eval datasets

## extraction/

Labelled job postings, one JSON file each. `expected` is an `ExtractedPosting`, so a
schema change breaks these loudly rather than silently scoring against the wrong shape.

**`reviewed` is false on every file here.** The labels were written alongside the code
rather than by an independent reader, and a label written by the same person who wrote
the extractor is not an independent measurement. The eval includes unreviewed labels in
its numbers and says so in the report; treat anything it prints as provisional until
that flag is true.

The set is deliberately varied in the ways that break extractors:

| file | what it tests |
|---|---|
| `backend-payments` | the ordinary case: clear headings, bullets, a benefits section to ignore |
| `data-engineer-seattle` | Lever's section titles, and `Workplace type` stated outside the prose |
| `ml-engineer-no-salary` | a range written `3-5 years`, and no salary at all — the hallucination test |
| `frontend-with-salary` | a stated salary range, employment type and remote policy |
| `sre-prose-requirements` | requirements written as prose with no bullets, which the rule-based baseline cannot see |

The last one is the point of having a baseline. A bullet-splitter scores near zero on
it; whether the model does better is exactly what the eval is for.

Ten more postings are needed to reach the fifteen the spec commits to, and they should
come from real listings rather than be written here.
