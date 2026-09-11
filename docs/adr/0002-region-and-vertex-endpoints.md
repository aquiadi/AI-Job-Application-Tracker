# 2. Split the deployment region from the Vertex AI endpoints

Date: 2026-09-11
Status: Accepted

## Context

The obvious configuration for a Google Cloud application is one `REGION` variable
used for everything. That does not work here, and the reason is not discoverable
without reading the model reference.

Checked on 2026-09-11 against
[the Vertex model pages](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models):

- **`gemini-3.5-flash`** (GA 2026-05-19, retirement 2027-05-19 or later) is served
  from `global`, the `us` and `eu` multi-regions, and a short list of regions:
  `northamerica-northeast1`, `europe-west2`, `europe-west3`, `asia-northeast1`,
  `asia-south1`, `asia-southeast1`, `australia-southeast1`. **`us-central1` is not
  among them.** Standard pay-as-you-go is available only on `global`, `us` and `eu`.
- **`gemini-embedding-001`** is a regional model, called through the regional
  `{region}-aiplatform.googleapis.com` endpoint. It is not served from `global`.
- **AlloyDB** is available in `us-central1` and most other regions, but not in
  `global` — `global` is not a region at all, it is a routing endpoint.

So a single region value has no correct setting. `us-central1` breaks generation.
`global` breaks embeddings and is not a location AlloyDB, Cloud Run, Cloud Tasks or
Cloud Scheduler will accept.

There is also a price difference. On the published
[pricing page](https://docs.cloud.google.com/vertex-ai/generative-ai/pricing),
Gemini 3.5 Flash input is $1.50 per million tokens on `global` and $1.65 on a
regional endpoint, with output at $9.00 against $9.90 — roughly 10% more to pin
generation to a region.

## Decision

Three separate settings, none of which defaults to the value of another:

| Setting | Default | What it locates |
|---|---|---|
| `GOOGLE_CLOUD_REGION` | `us-central1` | Cloud Run, AlloyDB, Cloud Tasks, Cloud Scheduler, Cloud Storage |
| `VERTEX_LOCATION` | `global` | Generative Gemini calls |
| `EMBEDDING_REGION` | `us-central1` | The embedding model |

`us-central1` is the deployment region because it carries AlloyDB and the embedding
model, it is one of the cheapest US regions for both Cloud Run and AlloyDB, and it is
the default a sandbox project is most likely to already have quota in. Generation
goes to `global` because that is where the model is served and because it is the
cheaper of the two options.

`scripts/sandbox_check.sh` probes all three independently — a `countTokens` call at
`VERTEX_LOCATION`, a one-token `predict` at `EMBEDDING_REGION`, and an AlloyDB
cluster list at `GOOGLE_CLOUD_REGION` — so a wrong combination fails in seconds
rather than at the first extraction.

## Alternatives

**One region, `us-central1`, with generation pinned there.** Simplest to explain and
keeps every byte in one region, which matters under a data-residency requirement.
Rejected because `gemini-3.5-flash` is not offered there at all; it is not a matter
of latency or price, the call returns 404.

**One region, `europe-west3` or `asia-northeast1`,** which serve both AlloyDB and
the generative models. This would genuinely collapse to a single value. Rejected
because it buys tidiness at the cost of about 10% on every generated token and a
narrower model catalogue, and because it pins a sandbox project to a region it may
have no quota in. If a data-residency requirement ever appears, this is the
alternative to revisit, and it is a configuration change rather than a code change.

**Route everything through `global`,** accepting that the database and the queues
cannot. Not actually an option; `global` is not a deployable location.

## Consequences

Three location variables are more to explain than one, and someone reading the
configuration for the first time will assume two of them are a mistake. The comments
in `.env.example` and in `settings.py` exist to answer that before it is asked.

Data crosses regions: a job description sent to the `global` endpoint may be
processed outside `us-central1`. `docs/privacy.md` records this. A deployment with a
residency requirement moves to the single-region alternative above.

Model retirement is now a two-line configuration change rather than a code change,
because every model id is read from the environment. `gemini-3.5-flash` retires no
earlier than 2027-05-19, which is comfortably past this project's deadline, but the
next model will move endpoints again and the split makes that survivable.
