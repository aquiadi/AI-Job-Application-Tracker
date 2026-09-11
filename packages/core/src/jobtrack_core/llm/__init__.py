"""The only place in this system that constructs a model client.

`LLM_BACKEND` selects the implementation. The default is `heuristic` in local
development, which is what lets the product be run and demonstrated with no Google
Cloud credentials, and `vertex` is required in cloud — a deployed process configured
with a local backend would serve rule-based extraction while reporting itself healthy,
so that combination fails at startup rather than in the interface.
"""

from __future__ import annotations

import enum
from pathlib import Path

from jobtrack_core.config.settings import Environment, Settings
from jobtrack_core.llm.cassette import CassetteLlmClient
from jobtrack_core.llm.client import (
    EmbeddingResult,
    EmbeddingTaskType,
    InvalidOutputError,
    LlmClient,
    LlmError,
    LlmResult,
    Usage,
)
from jobtrack_core.llm.heuristic import HeuristicLlmClient
from jobtrack_core.llm.prompts import Prompt, PromptError, RenderedPrompt, load_prompt
from jobtrack_core.llm.vertex import VertexLlmClient

__all__ = [
    "CassetteLlmClient",
    "EmbeddingResult",
    "EmbeddingTaskType",
    "HeuristicLlmClient",
    "InvalidOutputError",
    "LlmBackend",
    "LlmClient",
    "LlmError",
    "LlmResult",
    "Prompt",
    "PromptError",
    "RenderedPrompt",
    "Usage",
    "VertexLlmClient",
    "build_client",
    "load_prompt",
]


class LlmBackend(enum.StrEnum):
    VERTEX = "vertex"
    HEURISTIC = "heuristic"
    CASSETTE = "cassette"


def build_client(settings: Settings, *, cassette_dir: Path | None = None) -> LlmClient:
    """Construct the configured backend."""
    backend = LlmBackend(settings.llm_backend)

    if settings.environment is Environment.CLOUD and backend is not LlmBackend.VERTEX:
        raise LlmError(
            f"LLM_BACKEND={backend.value} with ENVIRONMENT=cloud. A deployed process "
            "must call Vertex; the local backends would serve rule-based output while "
            "reporting themselves healthy."
        )

    match backend:
        case LlmBackend.VERTEX:
            return VertexLlmClient.create(settings)
        case LlmBackend.HEURISTIC:
            return HeuristicLlmClient(embedding_dim=settings.vertex.embedding_dim)
        case LlmBackend.CASSETTE:
            directory = cassette_dir or Path(settings.llm_cassette_dir)
            return CassetteLlmClient(
                directory=directory, embedding_dim=settings.vertex.embedding_dim
            )
