"""Event handlers: what happens after a job is ingested.

These live in `core` rather than in the worker because both processes run them. In
cloud the worker consumes Pub/Sub and calls these; locally the in-process publisher
calls exactly the same functions. A handler that behaved differently in the two would
be a handler whose local testing proved nothing.

Every handler is idempotent and every handler opens its own tenant-scoped transaction
from the user id on the event. There is no ambient tenant here: a background process
has no request to inherit one from, so it has to be stated, and stating it is what the
row-level security policies then enforce.
"""

from __future__ import annotations

import json

from jobtrack_core.db.models import Job
from jobtrack_core.events import Event, EventType
from jobtrack_core.ingest.canonical import CanonicalPosting
from jobtrack_core.logs import get_logger
from jobtrack_core.pipeline import jobs as pipeline
from jobtrack_core.runtime import Runtime
from jobtrack_core.storage import StorageError

log = get_logger(__name__)


async def on_job_ingested(runtime: Runtime, event: Event) -> None:
    """Extract the posting, then embed what came out.

    Both steps run in one handler and in two transactions. One handler because there is
    nothing useful to do between them; two transactions because extraction can take
    seconds against a model, and holding a database transaction open across that call
    pins a connection for the duration.
    """
    posting = await _posting(runtime, event)
    if posting is None:
        return

    async with runtime.database.tenant(event.user_id) as session:
        job = await pipeline.extract(
            session,
            user_id=event.user_id,
            job_id=event.aggregate_id,
            posting=posting,
            client=runtime.llm,
        )
        failed = job.status.value == "failed"

    if failed:
        return

    async with runtime.database.tenant(event.user_id) as session:
        await pipeline.embed(
            session,
            user_id=event.user_id,
            job_id=event.aggregate_id,
            client=runtime.llm,
        )


async def _posting(runtime: Runtime, event: Event) -> CanonicalPosting | None:
    """Read back the canonical posting stored when the job was created."""
    async with runtime.database.tenant(event.user_id) as session:
        job = await session.get(Job, event.aggregate_id)
        uri = job.raw_gcs_uri if job is not None else None

    if uri is None:
        log.error("job_has_no_stored_posting", job_id=str(event.aggregate_id))
        return None

    try:
        raw = await runtime.raw.get(uri)
    except StorageError:
        log.exception("stored_posting_unreadable", job_id=str(event.aggregate_id))
        return None

    return CanonicalPosting.model_validate(json.loads(raw))


#: Wired by whatever is doing the dispatching. A dict rather than a decorator registry
#: so that the full set is visible in one place and a handler cannot be registered by
#: the side effect of an import.
HANDLERS: dict[EventType, str] = {EventType.JOB_INGESTED: "on_job_ingested"}


def register(runtime: Runtime, publisher: object) -> None:
    """Subscribe the in-process publisher to every handler.

    Local development only. In cloud the worker's HTTP endpoint is the subscriber and
    Pub/Sub does the dispatching.
    """
    from jobtrack_core.messaging import InProcessPublisher

    if not isinstance(publisher, InProcessPublisher):
        return

    publisher.subscribe(
        EventType.JOB_INGESTED.value,
        lambda event: on_job_ingested(runtime, event),
    )
