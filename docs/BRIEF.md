# AI Job Application Tracker: Product Brief

Owner: Aditya Jha
Status: v1 scope, September 2026

## Problem

Job searching asks for two things that pull against each other. Getting interviews is partly a volume game, but applicant tracking systems and recruiters screen for fit against a specific posting, so generic applications underperform. Tailoring a resume and cover letter properly for every role is slow, and across dozens of roles people end up either tailoring less or applying less.

Tracking makes it worse. Most searches run on a spreadsheet plus a dozen ATS portals. The spreadsheet records what happened. It can't tell you where applications stall or which kinds of roles actually respond.

## Users

The primary user is someone running an active search with many applications in flight: early-career engineers, recent graduates, and career changers in competitive fields like software and finance. They care about the quality of what they send and don't want a tool that sends things in their name without asking.

## Goals

1. Get from a job posting to a tailored, reviewable resume and cover letter in minutes, without inventing experience the user doesn't have.
2. Keep every application, stage change, and piece of correspondence in one place.
3. Make sure follow-ups happen on time: drafted and ready, with the user deciding whether to send.
4. Show where the search is working and where it isn't, with statistics that are honest about small samples.

## Non-goals (v1)

- Auto-applying to jobs or filling ATS forms.
- Sending email on the user's behalf.
- Scraping LinkedIn or Workday. Postings from those sources are pasted in.
- A job discovery feed or recommendations.
- Recruiter or employer features.
- Native mobile apps. v1 is a responsive web app.
- Non-English postings.

## User flow

1. **Sign in** with Google or LinkedIn.
2. **Build a master profile.** Upload a resume (PDF or DOCX) or a LinkedIn data export. The system extracts experience, projects, and skills into individual items. The user reviews and corrects them before they become the source of truth.
3. **Add a job.** Paste a URL (Greenhouse, Lever, Ashby, or a company careers page) or the posting text. The system normalizes it into a structured job record.
4. **See the fit.** Every requirement in the posting is marked covered, partial, or missing, alongside the profile item that covers it. The score summarizes that breakdown, and a short narrative explains the gaps.
5. **Tailor.** Generate a tailored resume and cover letter. Every rewritten bullet traces back to items in the master profile. The user reviews a diff against the master resume and exports PDF or DOCX.
6. **Track.** Applications move across a board with these stages: Saved, Applied, Screen, Technical, Onsite, Offer, plus Rejected, Withdrawn, and Ghosted. Each application has a timeline, notes, contacts, and correspondence.
7. **Follow up.** When an application sits in a stage too long, the user gets a drafted follow-up. They can edit it and send it from their own email, snooze it, or dismiss it.
8. **Learn.** Funnel conversion by stage, time in stage, and response rates by role family and source.

## Product principles

**The score is computed, not generated.** A number an LLM makes up can't be reproduced or explained. The fit score comes from matching each requirement against the profile using embeddings. The model only writes the explanation of the breakdown.

**Nothing is fabricated.** Tailored content can reword and re-weight what is in the profile. It can't add skills, tools, or metrics that aren't there. Content that fails this check is regenerated once, and otherwise dropped with a warning.

**Nothing goes out without the user.** Drafts are drafts. The user sends.

**Numbers come with their sample size.** A 40% difference over eight applications is noise. Every rate shows n and a confidence interval, and comparisons below a minimum sample size are not shown.

## Architecture

Google Cloud only.

| Component | Role |
|---|---|
| Cloud Run | `web` (Next.js), `api` (FastAPI), `worker` (event and task handlers), `migrate` (job) |
| Vertex AI (Gemini) | Structured extraction from resumes and postings; generation of tailored content, gap narratives, and follow-up drafts |
| Vertex AI embeddings | Vectors for profile items and job requirements |
| AlloyDB for PostgreSQL + pgvector | System of record and vector store |
| Pub/Sub | Domain events between services, with dead-letter topics |
| Cloud Tasks | Rate-limited per-item LLM work (batch tailoring, follow-up drafts) |
| Cloud Scheduler | Daily follow-up sweep, outbox relay backstop |
| Cloud Storage | Uploaded files, raw posting payloads, rendered documents |
| Identity Platform | Sign-in with Google and LinkedIn (OIDC) |
| Secret Manager, Artifact Registry, Cloud Build | Secrets, images, CI/CD |
| Cloud Logging, Cloud Trace | Structured logs and tracing |
| Firebase Cloud Messaging | Web push for follow-up drafts |

## Why AlloyDB

The data is relational at its core. A user has a profile made of items, applications reference jobs, jobs have requirements, and every application has an ordered history of stage changes. Stage transitions and their side effects need to commit atomically, which is ordinary Postgres territory.

The matching engine needs vectors for exactly the same entities. Computing a fit score means comparing a job's requirement vectors with the user's profile-item vectors. With pgvector in AlloyDB, that is one query, in one transaction, under the same row-level security policy that protects everything else. There is no sync job between an operational database and a separate vector store. There is also no second copy of resume content to secure, and no second tenant-isolation model to get wrong.

Cloud SQL for PostgreSQL also supports pgvector and would work at small scale. I chose AlloyDB for three things: the ScaNN index option as the vector tables grow, the columnar engine for analytics over stage history, and in-database model integration. The tradeoff is a higher minimum cost. I handle that by running Postgres locally for development and keeping the deployed instance small and easy to tear down.

## Pipelines

### Job posting ingestion

Postings arrive in very different shapes. Greenhouse, Lever, and Ashby expose public JSON, company career pages are HTML, and LinkedIn and Workday postings are pasted as text. Sources differ only in how they are fetched.

Every posting is extracted into one canonical, versioned schema using Gemini structured output. The schema covers:
- title, company, location, remote policy, employment type, seniority
- experience range and salary
- hard and soft skills, and responsibilities
- requirements, each marked must-have or nice-to-have

Fields the posting doesn't state stay null.

The extracted record is validated and stored together with its requirements in one transaction. An event then triggers a worker to embed the requirements and skills. Postings are deduplicated by content hash, and raw payloads are kept so records can be re-extracted when the schema changes. The user sees the status go from queued to ready, or to failed with a reason.

### Follow-up nudges

Each stage has a threshold in business days, measured in the user's timezone. The defaults are seven days after applying and two days after an interview, and users can change them.

1. A daily Cloud Scheduler job calls an internal sweep endpoint.
2. The sweep finds applications past their threshold and enqueues one Cloud Task each.
3. Before drafting, the task re-checks the application and exits if it has moved to another stage.
4. The draft is built from the job, the stage, the time elapsed, and prior correspondence. It never includes the user's contact details.
5. The draft is delivered in-app and by web push.

A database constraint guarantees at most one nudge per application per stage entry.

### Batch tailoring

Tailoring many saved jobs at once fans out to one Cloud Task per job. The queue is throttled below the Vertex AI quota, and progress streams back to the UI.

## Security and privacy

- **Auth.** Identity Platform issues ID tokens, and the API verifies them on every request. Internal endpoints only accept tokens from specific service accounts. LinkedIn sign-in provides identity only; profile data comes from the user's own export.
- **Tenant isolation.** Every user-owned table is protected by Postgres row-level security, enforced even for the table owner, with the current user set per transaction. A test verifies that one user can't read another user's rows.
- **Minimizing what the model sees.** Name, email, phone, address, and links are stored separately and never sent to Gemini. They are added back when documents are rendered. Logs contain no resume content.
- **Deletion and export.** Users can export everything or delete their account. Deletion removes database rows, stored files, and generated documents.
- **Model data handling.** Under Google's Vertex AI terms, customer data isn't used to train models without permission. Caching and retention settings are documented in `docs/privacy.md`.

## Scale

Traffic follows hiring cycles, with spikes around graduation season and layoff waves. Cloud Run scales the stateless services with load. Everything slow or rate-limited, including extraction, embedding, and generation, runs asynchronously. User requests don't block on model calls, and bursts stay within Vertex quotas.

## Measuring success

Model quality is measured by an eval suite that runs in CI:
- **Extraction:** skill precision and recall, per-field accuracy, and the rate of fields filled when the posting doesn't state them.
- **Fit score:** rank correlation with hand-labelled fit on profile/job pairs.
- **Tailoring:** share of bullets fully supported by the profile items they cite.

Product health is tracked with three measures:
- time from adding a job to having a tailored draft
- share of tailored bullets kept without edits
- share of nudges acted on

Targets are set after the first baseline run, not before.

## Risks and open questions

- Vertex AI model and quota availability in the chosen region.
- AlloyDB's minimum cost for a low-traffic deployment.
- The labelled eval set starts small, so early metrics will be noisy.
- Public job-board APIs can change or rate-limit without notice.
- Response-rate analytics depend on users recording outcomes, including silence. The Ghosted state and prompts to close out stale applications help, but the data will be incomplete.