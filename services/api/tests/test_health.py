from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jobtrack_api.main import app
from jobtrack_core.config import get_settings


@pytest.fixture
def client(isolated_environment: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # A project id is required: the token verifier uses it as both the expected
    # audience and the expected issuer, so there is no sensible default.
    isolated_environment.setenv("GOOGLE_CLOUD_PROJECT", "jobtrack-test-project")
    get_settings.cache_clear()
    with TestClient(app) as started:
        yield started
    get_settings.cache_clear()


def test_healthz_reports_the_service_and_environment(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "service": "api",
        "version": "0.1.0",
        "environment": "local",
    }


def test_openapi_operation_ids_are_readable(client: TestClient) -> None:
    # The web app's TypeScript client is generated from this document, so an
    # auto-derived id like `read_me_me_get` becomes a function name someone has to
    # read. Tags drive the naming instead.
    paths = client.get("/openapi.json").json()["paths"]

    assert paths["/healthz"]["get"]["operationId"] == "health_healthz"
    assert paths["/me"]["get"]["operationId"] == "me_read_me"


def test_the_api_refuses_to_start_without_a_project_id(
    isolated_environment: pytest.MonkeyPatch,
) -> None:
    # Starting with an empty project id would leave the verifier comparing the token
    # audience against "", which accepts tokens issued for any Firebase project.
    # Failing at startup is the only safe behaviour.
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="project_id"), TestClient(app):
            pass
    finally:
        get_settings.cache_clear()
