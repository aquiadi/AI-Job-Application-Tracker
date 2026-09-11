"""Publishing domain events, and the relay that drains the outbox.

The outbox is written in the same transaction as the change it describes, so the events
are already durable. What remains is getting them to a consumer, and that is a separate
problem with its own failure mode: the relay can publish a row and die before recording
that it did. So publication is at-least-once and every consumer deduplicates on
`event_id`. ADR 9 has the argument.

Two publishers, chosen the same way the model backend is. In cloud, Pub/Sub. Locally,
an in-process dispatcher that calls the handler directly — Pub/Sub's emulator exists,
but running the relay, a subscription and a second process to see a pasted job appear
is a great deal of moving machinery for a laptop, and the handler contract is identical
either way.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select, update

from jobtrack_core.db.models import Outbox
from jobtrack_core.db.session import Database, privileged_session
from jobtrack_core.events import Event
from jobtrack_core.logs import get_logger
from jobtrack_core.pipeline.jobs import to_event

log = get_logger(__name__)

#: Rows per relay pass. Small enough that one slow consumer does not hold a
#: transaction open across the whole backlog.
RELAY_BATCH = 50
#: How long a row may stay unpublished before the relay logs it as stuck.
STUCK_AFTER_ATTEMPTS = 5


class EventPublisher(Protocol):
    """Somewhere to send a published event."""

    async def publish(self, event: Event) -> None: ...

    async def close(self) -> None: ...


Handler = Callable[[Event], Awaitable[None]]


@dataclass(slots=True)
class InProcessPublisher:
    """Calls handlers directly, in a background task.

    Local development and tests. The task is tracked rather than fired and forgotten,
    because an un-awaited task that raises produces a warning at interpreter shutdown
    and nothing else — which is how a silently broken handler survives a whole session.
    """

    handlers: dict[str, list[Handler]] = field(default_factory=dict)
    _running: set[asyncio.Task[None]] = field(default_factory=set)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self.handlers.setdefault(event_type, []).append(handler)

    async def publish(self, event: Event) -> None:
        for handler in self.handlers.get(event.event_type.value, []):
            task = asyncio.create_task(self._run(handler, event))
            self._running.add(task)
            task.add_done_callback(self._running.discard)

    async def _run(self, handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            # The event stays published: redelivering it would need a broker, which is
            # precisely what this implementation does not have. The log is the record,
            # and the work is recoverable because the job's status is still `queued`.
            log.exception(
                "handler_failed",
                event_type=event.event_type.value,
                event_id=str(event.event_id),
            )

    async def drain(self) -> None:
        """Wait for in-flight handlers. Used by tests and by clean shutdown."""
        while self._running:
            await asyncio.gather(*tuple(self._running), return_exceptions=True)

    async def close(self) -> None:
        await self.drain()


@dataclass(slots=True)
class PubSubPublisher:
    """Publishes to a Pub/Sub topic, one message per event."""

    project_id: str
    topic: str
    _client: Any | None = field(default=None, repr=False)

    def _publisher(self) -> Any:
        if self._client is None:
            # Imported lazily so a local process never constructs a Pub/Sub client,
            # which looks for credentials at construction time.
            import google.cloud.pubsub_v1 as pubsub

            self._client = pubsub.PublisherClient()
        return self._client

    async def publish(self, event: Event) -> None:
        client = self._publisher()
        path = client.topic_path(self.project_id, self.topic)
        body = json.dumps(
            {
                "event_id": str(event.event_id),
                "event_type": event.event_type.value,
                "aggregate_type": event.aggregate_type,
                "aggregate_id": str(event.aggregate_id),
                "user_id": str(event.user_id),
                "payload": event.payload,
                "occurred_at": event.occurred_at.isoformat(),
            }
        ).encode("utf-8")

        # The client is synchronous and returns a concurrent.futures.Future. Waiting on
        # it in a thread keeps the event loop free, which matters because the relay
        # publishes a batch at a time.
        future = client.publish(path, body, **event.attributes())
        await asyncio.to_thread(future.result, 30)

    async def close(self) -> None:
        return None


@dataclass(slots=True)
class OutboxRelay:
    """Moves unpublished outbox rows to the publisher.

    Connects as `jobtrack_relay`, whose policy is the only one in the schema that reads
    across tenants, and whose grants stop at `outbox`.
    """

    database: Database
    publisher: EventPublisher
    batch_size: int = RELAY_BATCH

    async def drain_once(self) -> int:
        """Publish one batch. Returns how many rows were published."""
        async with privileged_session(self.database.sessions) as session:
            rows = list(
                (
                    await session.execute(
                        select(Outbox)
                        .where(Outbox.published_at.is_(None))
                        .order_by(Outbox.created_at)
                        .limit(self.batch_size)
                        # Two relay instances, or a Scheduler backstop firing while the
                        # continuous loop is mid-pass, would otherwise both publish the
                        # same rows. Skipping locked rows means the second one moves on
                        # rather than blocking or duplicating.
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )

            published = 0
            for row in rows:
                try:
                    await self.publisher.publish(to_event(row))
                except Exception as exc:
                    # Left unpublished on purpose, with the reason recorded. The next
                    # pass retries it; the attempt count is what makes a permanently
                    # failing row visible rather than silently retried forever.
                    await session.execute(
                        update(Outbox)
                        .where(Outbox.id == row.id)
                        .values(attempts=Outbox.attempts + 1, last_error=type(exc).__name__)
                    )
                    if row.attempts + 1 >= STUCK_AFTER_ATTEMPTS:
                        log.error(
                            "outbox_row_stuck",
                            event_id=str(row.id),
                            event_type=row.event_type,
                            attempts=row.attempts + 1,
                        )
                    continue

                await session.execute(
                    update(Outbox).where(Outbox.id == row.id).values(published_at=datetime.now(UTC))
                )
                published += 1

            return published

    async def run(self, *, interval_seconds: float = 1.0, stop: asyncio.Event) -> None:
        """Drain continuously until `stop` is set.

        Sleeps only when there was nothing to do, so a burst is drained at the speed of
        the publisher rather than one batch per tick.
        """
        while not stop.is_set():
            try:
                moved = await self.drain_once()
            except Exception:
                log.exception("relay_pass_failed")
                moved = 0

            if moved == 0:
                with_timeout = asyncio.wait_for(stop.wait(), timeout=interval_seconds)
                try:
                    await with_timeout
                except TimeoutError:
                    continue
