"""Process configuration, read once from the environment.

Three different Google Cloud locations show up in this system and they are not
interchangeable, so each one is a separate field rather than a single `region`:

* ``region`` — where Cloud Run, AlloyDB, Cloud Tasks and Cloud Scheduler live.
* ``vertex_location`` — where generative Gemini calls go. Gemini 3.x models are
  served from ``global`` and the multi-regions ``us``/``eu``, not from ordinary
  regions such as ``us-central1``.
* ``embedding_region`` — where the embedding model is served. ``gemini-embedding-001``
  is a regional model and is not available on the global endpoint.

Collapsing these into one value produces a 404 from Vertex at runtime, which is a
slow and confusing way to discover the constraint.
"""

from __future__ import annotations

import enum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Every settings model reads the same .env. Nested models do not inherit the parent's
# env_file, so leaving it off them means DB_* and GEMINI_* silently keep their defaults
# in local development while the top-level fields load correctly — the kind of
# half-working configuration that costs an hour to notice.
_ENV_FILE = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class Environment(enum.StrEnum):
    """Where the process is running, which decides how it reaches its dependencies."""

    LOCAL = "local"
    CLOUD = "cloud"


class DatabaseSettings(BaseSettings):
    """Connection details for AlloyDB, or for local Postgres standing in for it.

    In ``cloud`` the process connects through the AlloyDB Python Connector using IAM
    database authentication, so there is no password anywhere in this object. In
    ``local`` it connects to the docker compose Postgres over TCP with a password that
    is a fixture, not a secret.
    """

    model_config = SettingsConfigDict(**_ENV_FILE, env_prefix="DB_")

    name: str = "jobtrack"
    # The application role. It deliberately does not own the tables: row-level
    # security is only enforced against non-owners unless FORCE is set, and relying
    # on FORCE alone leaves no margin for error.
    app_user: str = "jobtrack_app"

    # Local only.
    host: str = "127.0.0.1"
    # 5433: the compose stack avoids the default so it can coexist with another
    # Postgres on the same machine.
    port: int = 5433
    password: SecretStr = SecretStr("jobtrack")

    # Cloud only. Format: projects/P/locations/R/clusters/C/instances/I
    alloydb_instance_uri: str = ""

    pool_size: int = 5
    max_overflow: int = 5
    pool_recycle_seconds: int = 1800


class VertexSettings(BaseSettings):
    """Model ids and endpoints for Vertex AI.

    Every model id is configurable because they retire on a published schedule.
    The defaults were checked against the Vertex model reference on 2026-09-11.
    """

    model_config = SettingsConfigDict(**_ENV_FILE)

    # Gemini 3.5 Flash-Lite: structured extraction is a constrained task where the
    # schema does most of the work, so it does not need the larger model.
    extract_model: str = Field(default="gemini-3.5-flash-lite", alias="GEMINI_EXTRACT_MODEL")
    # Gemini 3.5 Flash: generation is the quality-sensitive path.
    generate_model: str = Field(default="gemini-3.5-flash", alias="GEMINI_GENERATE_MODEL")
    embedding_model: str = Field(default="gemini-embedding-001", alias="EMBEDDING_MODEL")

    # 768 rather than the model's native 3072. pgvector indexes the `vector` type up
    # to 2,000 dimensions, and while `halfvec` reaches 4,000, 768 costs a quarter of
    # the storage and builds a far smaller index on the smallest AlloyDB shape.
    # See docs/adr/0004-model-and-embedding-selection.md.
    embedding_dim: Annotated[int, Field(ge=1, le=2000)] = Field(default=768, alias="EMBEDDING_DIM")

    location: str = Field(default="global", alias="VERTEX_LOCATION")
    embedding_region: str = Field(default="us-central1", alias="EMBEDDING_REGION")

    # Guard rail, not a budget. A single call that blows past this is a bug in the
    # caller, and failing loudly beats discovering it on the invoice.
    max_output_tokens: int = Field(default=8192, alias="GEMINI_MAX_OUTPUT_TOKENS")
    request_timeout_seconds: float = Field(default=120.0, alias="GEMINI_TIMEOUT_SECONDS")


class Settings(BaseSettings):
    """Everything the process needs to know, resolved at import time."""

    model_config = SettingsConfigDict(**_ENV_FILE, case_sensitive=False)

    environment: Environment = Environment.LOCAL
    service_name: Literal["api", "worker", "migrate", "cli"] = "cli"
    log_level: str = "INFO"

    project_id: str = Field(default="", alias="GOOGLE_CLOUD_PROJECT")
    project_number: str = Field(default="", alias="GOOGLE_CLOUD_PROJECT_NUMBER")
    region: str = Field(default="us-central1", alias="GOOGLE_CLOUD_REGION")

    # Identity Platform. The audience of a Firebase ID token is the project id.
    auth_emulator_host: str = Field(default="", alias="FIREBASE_AUTH_EMULATOR_HOST")

    pubsub_emulator_host: str = Field(default="", alias="PUBSUB_EMULATOR_HOST")

    gcs_uploads_bucket: str = Field(default="", alias="GCS_UPLOADS_BUCKET")
    gcs_raw_bucket: str = Field(default="", alias="GCS_RAW_BUCKET")
    gcs_artifacts_bucket: str = Field(default="", alias="GCS_ARTIFACTS_BUCKET")

    # Service accounts allowed to call /internal/*. Comma-separated emails.
    internal_invoker_accounts: str = Field(default="", alias="INTERNAL_INVOKER_ACCOUNTS")

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    vertex: VertexSettings = Field(default_factory=VertexSettings)

    @model_validator(mode="after")
    def _require_cloud_fields(self) -> Settings:
        if self.environment is not Environment.CLOUD:
            return self
        missing = [
            name
            for name, value in (
                ("GOOGLE_CLOUD_PROJECT", self.project_id),
                ("DB_ALLOYDB_INSTANCE_URI", self.database.alloydb_instance_uri),
                ("GCS_UPLOADS_BUCKET", self.gcs_uploads_bucket),
                ("GCS_RAW_BUCKET", self.gcs_raw_bucket),
                ("GCS_ARTIFACTS_BUCKET", self.gcs_artifacts_bucket),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"ENVIRONMENT=cloud requires: {', '.join(missing)}. "
                "See .env.example for the full list."
            )
        return self

    @property
    def is_local(self) -> bool:
        return self.environment is Environment.LOCAL

    @property
    def internal_invokers(self) -> frozenset[str]:
        """Service account emails permitted to call internal endpoints."""
        return frozenset(
            part.strip() for part in self.internal_invoker_accounts.split(",") if part.strip()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed once.

    Cached so that a bad environment fails at the first call rather than
    intermittently, and so tests can clear it explicitly.
    """
    return Settings()
