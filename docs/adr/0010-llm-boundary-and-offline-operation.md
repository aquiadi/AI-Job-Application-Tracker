# 0010. Put every model call behind one protocol, and make the system work without it

Date: 2026-09-11
Status: Accepted

## Context

Two forces pull against each other here.

The first is that this repository has to be developed, tested and demonstrated on a
machine with no Google Cloud credentials. `gcloud` has no active account, the sandbox
project has no billing, and the audition deadline is 30 September 2026. Work cannot
stop while that is resolved.

The second is that a system which only works when a paid API answers is a system whose
failure modes are invisible until the day they matter. Vertex AI returns 429 under
quota pressure, 404 when a model id retires, and occasionally malformed JSON despite a
`response_schema`. Discovering those paths in production is expensive.

Both forces point the same way: the model call has to be an interface with more than
one implementation, and the system has to have defined behaviour when the model is
absent rather than raising whatever the SDK happened to raise.

There is also a measurement argument. An extraction eval that reports "Gemini scored
0.82 on requirement recall" is close to meaningless on its own. 0.82 against what? A
deterministic baseline that splits a posting on its bullet markers is the number that
makes the model's contribution legible, and building one costs an afternoon.

## Decision

Every Gemini and embedding call goes through `LlmClient`, a Protocol in
`packages/core/.../llm/` with two methods: `generate_structured` and `embed`. No other
module constructs a `genai.Client`.

Three implementations ship:

- `VertexLlmClient` — the real one. `google-genai` with `vertexai=True`, Pydantic
  `response_schema`, retry-once-with-the-validation-error, token accounting from
  `usage_metadata`, and a cost estimate per call.
- `CassetteLlmClient` — replays recorded responses keyed on
  `(prompt_id, prompt_version, sha256 of input)`. A miss is an error, never a silent
  fallthrough. This is what unit tests use, so they need no credentials and no network.
- `HeuristicLlmClient` — a deterministic rule-based implementation. It is a real
  algorithm, not a stub: it segments a posting on bullet markers and requirement verbs,
  classifies must versus nice on modal phrasing, and produces embeddings by hashed
  character-trigram projection.

The backend is selected by `LLM_BACKEND`, defaulting to `heuristic` in `local` and
required to be `vertex` in `cloud`. A `cloud` process configured with a non-Vertex
backend fails at startup.

Anything produced by a non-Vertex backend is labelled as such in the database: every
`llm_calls` row records the model id it used, and `heuristic` is a model id.

## Alternatives

**Call Vertex or fail.** Simplest, and what most projects do. Rejected because it makes
the entire product undemonstrable until billing is resolved, and because it leaves the
degraded path untested.

**Mock the SDK in tests and call Vertex everywhere else.** This is the common shape,
and it tests the mock rather than the seam. A `unittest.mock` patch of
`client.aio.models.generate_content` asserts nothing about whether the caller handles a
response with `candidates=[]`, which is a real Vertex response.

**A small local model through Ollama.** Genuinely tempting, and it would give better
offline extraction than heuristics. Rejected on the Google-Cloud-only constraint in the
brief, and because a multi-gigabyte model download is a worse onboarding story than a
200-line function.

**Record/replay only, with no heuristic backend.** Rejected because a cassette can only
answer for inputs recorded earlier. A person pasting a posting the repository has never
seen would get an error, which is not a working product. Cassettes remain the right
answer for tests, where the input set is fixed by construction.

## Consequences

The product is usable end to end with no credentials, at reduced extraction quality,
and the reduction is visible rather than hidden: the interface shows which backend
produced each posting's requirements.

The extraction eval gains a baseline for free. `evals/` runs the same dataset through
both backends, so the report says what Gemini is worth on this task rather than only
what it scores.

The cost is a second implementation of a contract that could have had one, and the
discipline of keeping it honest. `HeuristicLlmClient` must never be quietly improved
into something that looks like an extraction model; its job is to be a floor.

Reversing this is cheap in one direction and expensive in the other. Deleting the
heuristic backend is an afternoon. Removing the protocol and calling `genai` directly
from handlers would mean every test needs credentials, which is the state this decision
exists to avoid.
