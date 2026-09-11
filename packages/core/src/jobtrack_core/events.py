"""Domain events, and the envelope they travel in.

An event is written to `outbox` in the same transaction as the change it describes, so
there is no state in which a job exists and the event announcing it does not. ADR 9
records why that matters more than it might appear: the alternative, publishing after
commit, loses the event whenever the process dies in the gap.

Event names are `<aggregate>.<past tense verb>` and are part of the wire contract. A
consumer deduplicates on `event_id`, which is the outbox row's primary key, because the
relay publishes at least once by construction — a row can be published and the process
die before `published_at` is written.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class EventType(enum.StrEnum):
    """Every event this system publishes."""

    JOB_INGESTED = "job.ingested"
    JOB_EXTRACTED = "job.extracted"
    JOB_READY = "job.ready"
    JOB_FAILED = "job.failed"
    PROFILE_IMPORTED = "profile.imported"
    PROFILE_ITEMS_CHANGED = "profile.items_changed"
    APPLICATION_STAGE_CHANGED = "application.stage_changed"
    ARTIFACT_GENERATED = "artifact.generated"


#: Aggregate names, used for `outbox.aggregate_type`.
JOB = "job"
PROFILE = "profile"
APPLICATION = "application"
ARTIFACT = "artifact"


@dataclass(frozen=True, slots=True)
class Event:
    """One published domain event.

    `user_id` travels with the event because a consumer has to set its own tenant
    context before it can touch any row the event refers to. It is not a hint: without
    it, a handler has no legitimate way to read the aggregate at all.
    """

    event_id: uuid.UUID
    event_type: EventType
    aggregate_type: str
    aggregate_id: uuid.UUID
    user_id: uuid.UUID
    payload: dict[str, Any]
    occurred_at: datetime

    def attributes(self) -> dict[str, str]:
        """Pub/Sub message attributes. Strings only, by the transport's rules."""
        return {
            "event_id": str(self.event_id),
            "event_type": self.event_type.value,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": str(self.aggregate_id),
            "user_id": str(self.user_id),
        }
