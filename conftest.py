"""Test-suite-wide isolation.

Settings are read from the process environment and from a ``.env`` file in the
working directory. Both are ambient, so without this fixture a developer's own
``.env`` changes test outcomes and a test that sets an environment variable leaks
into the next one. Every test therefore starts in an empty directory with the
project's environment variables removed.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# Prefixes owned by this project. Anything matching is cleared before each test.
_OWNED_PREFIXES: tuple[str, ...] = (
    "DB_",
    "EMBEDDING_",
    "ENVIRONMENT",
    "FIREBASE_",
    "GCS_",
    "GEMINI_",
    "GOOGLE_CLOUD_",
    "INTERNAL_INVOKER_",
    "LOG_LEVEL",
    "PUBSUB_",
    "SERVICE_NAME",
    "VERTEX_",
)


@pytest.fixture(autouse=True)
def isolated_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[pytest.MonkeyPatch]:
    """Clear project environment variables and run from a directory with no .env."""
    for name in list(os.environ):
        if name.startswith(_OWNED_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    yield monkeypatch
