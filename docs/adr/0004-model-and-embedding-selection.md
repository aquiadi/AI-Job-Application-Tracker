# 4. Two generative models, and gemini-embedding-001 at 768 dimensions

Date: 2026-09-11
Status: Accepted

## Context

Three model choices have to be made before any of the pipelines can be written, and
two of them are hard to reverse once data exists.

Prices below are from the
[Vertex AI pricing page](https://docs.cloud.google.com/vertex-ai/generative-ai/pricing)
on the `global` endpoint, checked 2026-09-11, in USD per million tokens:

| Model | Input | Output | Cached input |
|---|---|---|---|
| gemini-3.5-flash | 1.50 | 9.00 | 0.15 |
| gemini-3.5-flash-lite | 0.30 | 2.50 | 0.03 |
| gemini-embedding (per 1,000 tokens) | 0.00015 | no charge | — |

Embedding input therefore works out at $0.15 per million tokens.

The two generative jobs are not alike. Extraction turns a job posting into a strict
schema: the `response_schema` does most of the structural work, the answer is
extractive rather than creative, and the eval measures whether unstated fields stay
null. Generation writes tailored bullets and gap narratives that a human reads and
judges. The first is a constrained transformation; the second is the part of the
product people will form an opinion about.

For embeddings there are two candidate families:

- **`gemini-embedding-001`** — documented with the `task_type` enum
  (`SEMANTIC_SIMILARITY`, `RETRIEVAL_QUERY`, `RETRIEVAL_DOCUMENT`, and five others),
  `outputDimensionality` down from 3072, 2048-token sequence limit, served
  regionally.
- **`gemini-embedding-2`** — GA 2026-04-22, natively multimodal, 3072 dimensions,
  8192-token limit, auto-normalising when truncated. It takes free-text *task
  instructions* (`task:search result`) rather than the enum, and the enum
  documentation explicitly lists only `text-embedding-005`,
  `text-multilingual-embedding-002` and `gemini-embedding-001` as supporting
  `task_type`. It is served from `global`, `us` and `eu` only.

## Decision

**`gemini-3.5-flash-lite` for extraction, `gemini-3.5-flash` for generation.**
Extraction is the high-volume path — every posting, plus every re-extraction when
the schema version bumps — and it is the path where the schema, not the model,
carries the quality. Generation runs perhaps twice per application and is read
closely. Five times the input price and 3.6 times the output price is worth paying
there and not worth paying on extraction. Both are read from the environment, and
the extraction eval measures whether flash-lite actually holds up; if its
hallucinated-field rate is materially worse, the fix is one variable.

**`gemini-embedding-001`, 768 dimensions, `SEMANTIC_SIMILARITY`.** 768 is forced by
pgvector: its HNSW index rejects vectors above 2000 dimensions, so the native 3072
would leave the vectors unindexable. Matryoshka truncation to 768 is supported, and
truncated vectors are re-normalised in application code before they are stored, so
cosine distance and inner product agree.

`SEMANTIC_SIMILARITY` is the default because of what is actually being compared. A
job requirement ("distributed transactions and idempotent processing") and a profile
item ("designed the idempotent ledger write path") are the same genre of text: two
declarative statements about capability, of similar length and register. That is
textual similarity, not the question-to-passage asymmetry that `RETRIEVAL_QUERY` and
`RETRIEVAL_DOCUMENT` are tuned for.

That reasoning is plausible rather than proven, so the task type is configuration,
and the M3 calibration eval decides it: the same labelled profile/posting pairs are
scored under `SEMANTIC_SIMILARITY` and under the asymmetric pairing (requirement as
`RETRIEVAL_QUERY`, profile item as `RETRIEVAL_DOCUMENT`), and whichever gives the
better Spearman correlation against hand-labelled fit wins. The number goes in the
eval report either way.

Because the task type changes the vectors, every vector row stores
`embedding_task_type` alongside `embedding_model` and `embedding_dim`. All three
together identify a vector space; a change to any of them is a re-embed job.

## Alternatives

**`gemini-3.5-flash` for extraction too.** Simpler — one model, one price, one set of
eval numbers. Rejected because extraction is the volume path and the schema does the
work. If the eval shows flash-lite dropping fields or inventing them, this is the
first thing to change.

**`gemini-embedding-2`.** Newer, multimodal, longer context, and it auto-normalises
truncated vectors so the application would not have to. Rejected for now because its
task control is free text rather than the documented enum, which makes "pick the task
type deliberately and calibrate it" much harder to do rigorously; because it is not
served regionally, adding a third Vertex location to the configuration; and because
nothing in this product is multimodal — resumes arrive as PDFs but are parsed to text
before they are ever embedded. The `embedding_model` column means switching later is
a backfill job, not a migration.

**Store the full 3072 dimensions and use a flat index.** Exact search over a few
thousand vectors is fast, and it avoids the truncation question entirely. Rejected
because it forecloses HNSW without measuring anything, and because four times the
storage per vector is a real cost on the smallest AlloyDB shape.

## Consequences

Two model ids to keep current instead of one, and two sets of cost numbers in the
cost report — which is the point, since cost per JD ingested and cost per tailored
resume are reported separately.

The fit score's quality now depends on a task-type choice that is defended by
argument and settled by measurement. Until the M3 calibration runs, that is an open
question, and the eval report says so rather than the README claiming otherwise.

Every stored vector is tied to a triple of (model, dimensions, task type). Changing
any one of them invalidates every comparison against older rows, so the re-embed job
is not optional cleanup — it is the migration.
