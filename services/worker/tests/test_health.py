from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jobtrack_core.config import get_settings
from jobtrack_worker.main import app


@pytest.fixture
def client(isolated_environment: pytest.MonkeyPatch) -> Iterator[TestClient]:
    get_settings.cache_clear()
    with TestClient(app) as started:
        yield started
    get_settings.cache_clear()


def test_healthz_reports_the_service(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["service"] == "worker"


def test_the_worker_publishes_no_openapi_document(client: TestClient) -> None:
    # Its endpoints are callable only by Pub/Sub and Cloud Tasks with a service
    # account token; advertising a schema for them is misleading.
    assert client.get("/openapi.json").status_code == 404
