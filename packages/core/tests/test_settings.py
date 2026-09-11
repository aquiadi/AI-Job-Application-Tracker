from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobtrack_core.config import Environment, Settings, get_settings

_COMPLETE_CLOUD_ENV: dict[str, str] = {
    "ENVIRONMENT": "cloud",
    "GOOGLE_CLOUD_PROJECT": "jobtrack-sandbox",
    "DB_ALLOYDB_INSTANCE_URI": (
        "projects/jobtrack-sandbox/locations/us-central1/clusters/jobtrack/instances/primary"
    ),
    "GCS_UPLOADS_BUCKET": "jobtrack-uploads",
    "GCS_RAW_BUCKET": "jobtrack-raw",
    "GCS_ARTIFACTS_BUCKET": "jobtrack-artifacts",
}


def _load(env: pytest.MonkeyPatch, **values: str) -> Settings:
    """Build settings the way a deployed process does: from environment variables."""
    for key, value in values.items():
        env.setenv(key, value)
    return Settings()


class TestCloudRequirements:
    def test_local_environment_needs_no_configuration(
        self, isolated_environment: pytest.MonkeyPatch
    ) -> None:
        settings = _load(isolated_environment)

        assert settings.environment is Environment.LOCAL
        assert settings.is_local

    def test_cloud_environment_names_every_missing_variable_at_once(
        self, isolated_environment: pytest.MonkeyPatch
    ) -> None:
        # One error listing all four beats four deploys each revealing the next.
        with pytest.raises(ValidationError) as caught:
            _load(isolated_environment, ENVIRONMENT="cloud", GOOGLE_CLOUD_PROJECT="p")

        message = str(caught.value)
        assert "DB_ALLOYDB_INSTANCE_URI" in message
        assert "GCS_UPLOADS_BUCKET" in message
        assert "GCS_RAW_BUCKET" in message
        assert "GCS_ARTIFACTS_BUCKET" in message

    def test_cloud_environment_accepts_a_complete_configuration(
        self, isolated_environment: pytest.MonkeyPatch
    ) -> None:
        settings = _load(isolated_environment, **_COMPLETE_CLOUD_ENV)

        assert not settings.is_local
        assert settings.database.alloydb_instance_uri.endswith("/instances/primary")


class TestNestedSettingsReadTheEnvironment:
    """Nested BaseSettings do not inherit the parent's sources; these prove they read."""

    def test_database_settings_pick_up_their_prefixed_variables(
        self, isolated_environment: pytest.MonkeyPatch
    ) -> None:
        settings = _load(isolated_environment, DB_NAME="jobtrack_test", DB_PORT="5433")

        assert settings.database.name == "jobtrack_test"
        assert settings.database.port == 5433

    def test_model_ids_are_overridable_because_models_retire(
        self, isolated_environment: pytest.MonkeyPatch
    ) -> None:
        settings = _load(
            isolated_environment,
            GEMINI_EXTRACT_MODEL="gemini-4-flash-lite",
            GEMINI_GENERATE_MODEL="gemini-4-flash",
            EMBEDDING_MODEL="gemini-embedding-3",
        )

        assert settings.vertex.extract_model == "gemini-4-flash-lite"
        assert settings.vertex.generate_model == "gemini-4-flash"
        assert settings.vertex.embedding_model == "gemini-embedding-3"


class TestVertexLocations:
    def test_generative_and_embedding_endpoints_default_apart(
        self, isolated_environment: pytest.MonkeyPatch
    ) -> None:
        # Gemini 3.x generative models are not served from ordinary regions, and
        # gemini-embedding-001 is not served from the global endpoint. One shared
        # location value cannot satisfy both.
        settings = _load(isolated_environment)

        assert settings.vertex.location == "global"
        assert settings.vertex.embedding_region == "us-central1"

    def test_embedding_dim_is_capped_at_the_hnsw_index_limit(
        self, isolated_environment: pytest.MonkeyPatch
    ) -> None:
        # pgvector's HNSW index rejects more than 2000 dimensions. A dimension that
        # embeds fine and then fails at index creation must not parse.
        with pytest.raises(ValidationError):
            _load(isolated_environment, EMBEDDING_DIM="3072")

    def test_embedding_dim_is_overridable_below_the_limit(
        self, isolated_environment: pytest.MonkeyPatch
    ) -> None:
        assert _load(isolated_environment, EMBEDDING_DIM="1536").vertex.embedding_dim == 1536


class TestInternalInvokers:
    def test_empty_means_no_service_account_may_call_internal_endpoints(
        self, isolated_environment: pytest.MonkeyPatch
    ) -> None:
        assert _load(isolated_environment).internal_invokers == frozenset()

    def test_whitespace_and_trailing_commas_are_tolerated(
        self, isolated_environment: pytest.MonkeyPatch
    ) -> None:
        accounts = " sweeper@p.iam.gserviceaccount.com , relay@p.iam.gserviceaccount.com ,"
        settings = _load(isolated_environment, INTERNAL_INVOKER_ACCOUNTS=accounts)

        assert settings.internal_invokers == {
            "sweeper@p.iam.gserviceaccount.com",
            "relay@p.iam.gserviceaccount.com",
        }


def test_get_settings_is_cached(isolated_environment: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
