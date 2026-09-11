# 9. Write domain events to an outbox table, not straight to Pub/Sub

Date: 2026-09-11
Status: Accepted

## Context

Several things in this system have to happen after a state change but not inside the
request that caused it. A job is extracted and its requirements need embedding. An
application moves to Applied and the nudge clock starts. A profile item is edited and
every saved job's fit score is now stale.

The obvious implementation is to commit the transaction and then publish to Pub/Sub.
That is two operations across two systems with no shared transaction, and there are
two ways it goes wrong. The publish succeeds and the transaction rolls back, so a
worker embeds requirements for a job that does not exist. Or the transaction commits
and the publish fails — the process is killed, the network blips, Pub/Sub returns a
503 — and the job sits at `extracting` forever with nothing scheduled to move it.

The second failure is the common one and the worse one, because it is silent. Nothing
errors. A row is simply stuck, and the only way to find it is a user asking why their
job never finished.

## Decision

The domain event is a row in an `outbox` table, written in the same transaction as the
state change it describes. Either both land or neither does.

A relay reads unpublished rows, publishes them to Pub/Sub, and stamps `published_at`.
It runs after commit in the request path for latency, and Cloud Scheduler calls it
every minute as a backstop so a relay that crashed mid-publish is not a permanent
outage — it is a sixty-second delay.

That design publishes at least once by construction: a row can be published and then
the process die before `published_at` is written, so the backstop publishes it again.
Consumers are therefore idempotent on `event_id`, which is the primary key of the
outbox row and travels in the message attributes. Every subscription has a dead-letter
topic and a retry policy, so a message that fails repeatedly stops being retried
forever and becomes something inspectable.

## Alternatives

**Dual writes — commit, then publish.** Less code, no relay, no backstop, no extra
table. Rejected for the silent-stall failure above. A system whose failure mode is "a
row is stuck and nobody is told" is one I would rather not operate.

**Publish first, then commit.** Reverses which failure you get: now you publish events
for work that never happened. Worse, because a consumer acting on a phantom event can
write real rows.

**Listen/notify from Postgres.** `LISTEN`/`NOTIFY` is transactional, which solves the
consistency problem elegantly. Rejected because notifications are fire-and-forget with
no persistence: a listener that is down when the notification fires never learns about
it, and Cloud Run scales the worker to zero. The outbox row survives the worker being
absent, which is the whole point.

**Change data capture off the WAL.** The industrial answer, and correct at scale.
Rejected as far too much machinery for this: a Datastream pipeline to operate and pay
for, to solve a problem a table and a cron job solve.

## Consequences

There is a table that grows and needs pruning. Published rows older than some retention
window get deleted; until that job exists, the table grows without bound, which is fine
at demo volume and is the kind of thing that bites six months later. It is on the
roadmap rather than in M1.

Every consumer now has to be idempotent, and that is a property I have to actively
maintain rather than one I get. The discipline is that a handler either checks
`event_id` against what it has already processed, or its effect is naturally
idempotent — setting a status to `ready` twice is harmless; appending a `stage_events`
row twice is not.

Latency has a floor. In the happy path the relay publishes immediately after commit, so
it is milliseconds. When the relay fails, it is up to a minute. That is acceptable for
embedding and nudges, and it would not be for anything a user watches spin.

The reward is that "the database says this happened" and "an event was published for
it" cannot disagree. Given that the database is the system of record for someone's job
search, I would rather pay a table and a cron job for that than debug a stuck row.
