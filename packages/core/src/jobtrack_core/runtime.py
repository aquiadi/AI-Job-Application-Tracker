"""The dependencies a process needs, built once and handed around explicitly.

Every one of these has a local implementation that costs nothing and a cloud
implementation that needs credentials, and the selection happens here and nowhere else.
That is what lets the same handler code run under `make dev` against Postgres and a
directory on disk, and under Cloud Run against AlloyDB, Cloud Storage and Pub/Sub,
with no branch inside the handler.

Construction is explicit rather than a global. A module-level singleton would be read
at import time, which is before tests have finished setting the environment, and the
resulting configuration is whatever happened to be true when the first import ran.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from jobtrack_core.config.settings import Environment, Settings
from jobtrack_core.db.session import Database, create_database, create_relay_database
from jobtrack_core.llm import build_client
from jobtrack_core.llm.client import LlmClient
from jobtrack_core.messaging import EventPublisher, InProcessPublisher, OutboxRelay, PubSubPublisher
from jobtrack_core.storage import GcsObjectStore, LocalObjectStore, ObjectStore

#: Where the local object store writes. Under the repository rather than a temporary
#: directory, so an uploaded resume survives a restart during development.
LOCAL_STORAGE_ROOT = Path(".localstore")

#: The topic every domain event is published to. One topic with an `event_type`
#: attribute, rather than a topic per event: subscriptions filter on the attribute, and
#: adding an event type then needs no new infrastructure.
EVENTS_TOPIC = "jobtrack-events"


@dataclass(slots=True)
class Runtime:
    """Everything a request or a handler needs, already constructed."""

    settings: Settings
    database: Database
    llm: LlmClient
    uploads: ObjectStore
    raw: ObjectStore
    artifacts: ObjectStore
    publisher: EventPublisher
    relay: OutboxRelay
    _relay_database: Database
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _relay_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def start_relay(self, *, interval_seconds: float = 1.0) -> None:
        """Run the relay inside this process.

        Local development only. In cloud the relay is its own Cloud Run service with a
        Cloud Scheduler backstop, because a relay that lives inside the API stops when
        the API scales to zero — which is exactly when a backlog would build.
        """
        if self._relay_task is None:
            self._relay_task = asyncio.create_task(
                self.relay.run(interval_seconds=interval_seconds, stop=self._stop)
            )

    async def close(self) -> None:
        self._stop.set()
        if self._relay_task is not None:
            await self._relay_task
            self._relay_task = None
        await self.publisher.close()
        await self.llm.close()
        await self.database.close()
        await self._relay_database.close()


def build_runtime(settings: Settings) -> Runtime:
    """Construct every dependency for this environment."""
    database = create_database(settings)
    relay_database = create_relay_database(settings)
    publisher = _publisher(settings)

    return Runtime(
        settings=settings,
        database=database,
        llm=build_client(settings),
        uploads=_store(settings, settings.gcs_uploads_bucket, "uploads"),
        raw=_store(settings, settings.gcs_raw_bucket, "raw"),
        artifacts=_store(settings, settings.gcs_artifacts_bucket, "artifacts"),
        publisher=publisher,
        relay=OutboxRelay(database=relay_database, publisher=publisher),
        _relay_database=relay_database,
    )


def _publisher(settings: Settings) -> EventPublisher:
    if settings.environment is Environment.LOCAL:
        return InProcessPublisher()
    return PubSubPublisher(project_id=settings.project_id, topic=EVENTS_TOPIC)


def _store(settings: Settings, bucket: str, kind: str) -> ObjectStore:
    if settings.environment is Environment.LOCAL:
        return LocalObjectStore(root=LOCAL_STORAGE_ROOT / kind)
    return GcsObjectStore(bucket_name=bucket)
