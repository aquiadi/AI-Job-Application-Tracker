"""Reaching the process-wide runtime from a request.

The runtime is built once in the lifespan and stored on `app.state`. These exist so a
router depends on `LlmClient` or `ObjectStore` rather than on FastAPI's application
object, which keeps the handlers testable with a plain object in place of the app.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from jobtrack_core.messaging import EventPublisher
from jobtrack_core.runtime import Runtime
from jobtrack_core.storage import ObjectStore


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


def get_raw_store(runtime: Annotated[Runtime, Depends(get_runtime)]) -> ObjectStore:
    return runtime.raw


def get_publisher(runtime: Annotated[Runtime, Depends(get_runtime)]) -> EventPublisher:
    return runtime.publisher


AppRuntime = Annotated[Runtime, Depends(get_runtime)]
RawStore = Annotated[ObjectStore, Depends(get_raw_store)]
