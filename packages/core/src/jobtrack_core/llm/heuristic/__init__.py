"""The offline backend: deterministic, free, and honest about what it is.

It satisfies :class:`LlmClient` without calling anything. Generation dispatches on
`prompt_id` to a rule-based implementation, and a prompt with no implementation raises
rather than returning an empty object — a backend that silently produced nothing would
look like a model that found nothing, which is the one failure mode worth preventing.

Every result it produces is recorded in `llm_calls` with model id `heuristic`, so no
row ever claims a Gemini model wrote something this did.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel

from jobtrack_core.ingest.extraction import ExtractedPosting
from jobtrack_core.llm.client import (
    EmbeddingResult,
    EmbeddingTaskType,
    LlmError,
    LlmResult,
    Usage,
)
from jobtrack_core.llm.heuristic.embed import embed_text
from jobtrack_core.llm.heuristic.resume import parse as parse_resume
from jobtrack_core.llm.heuristic.segment import HEURISTIC_MODEL, extract
from jobtrack_core.llm.prompts import RenderedPrompt
from jobtrack_core.profile.schemas import ParsedResume

__all__ = [
    "HEURISTIC_MODEL",
    "HeuristicLlmClient",
    "embed_text",
    "extract",
    "parse_resume",
]

#: Maps a prompt id to the function that answers it from its rendered variables.
Handler = Callable[[RenderedPrompt], BaseModel]


def _extract_jd(prompt: RenderedPrompt) -> ExtractedPosting:
    return extract(
        prompt.values.get("posting", ""),
        title=prompt.values.get("title") or None,
        company=prompt.values.get("company") or None,
    )


def _parse_resume(prompt: RenderedPrompt) -> ParsedResume:
    return parse_resume(prompt.values.get("resume", ""))


DEFAULT_HANDLERS: dict[str, Handler] = {
    "extract_jd": _extract_jd,
    "parse_resume": _parse_resume,
}


@dataclass(slots=True)
class HeuristicLlmClient:
    """Rule-based generation and hashed embeddings. Calls nothing, costs nothing."""

    embedding_dim: int = 768
    handlers: dict[str, Handler] = field(default_factory=lambda: dict(DEFAULT_HANDLERS))

    @property
    def generate_model(self) -> str:
        return HEURISTIC_MODEL

    @property
    def embedding_model(self) -> str:
        return HEURISTIC_MODEL

    async def generate_structured[T: BaseModel](
        self,
        prompt: RenderedPrompt,
        schema: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> LlmResult[T]:
        handler = self.handlers.get(prompt.prompt_id)
        if handler is None:
            raise LlmError(
                f"the heuristic backend has no implementation of {prompt.prompt_id!r}. "
                "Set LLM_BACKEND=vertex, or add a handler."
            )

        started = time.perf_counter()
        produced = handler(prompt)
        if not isinstance(produced, schema):
            raise LlmError(
                f"the heuristic handler for {prompt.prompt_id!r} returned "
                f"{type(produced).__name__}, not {schema.__name__}"
            )

        return LlmResult(
            value=produced,
            model=HEURISTIC_MODEL,
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            # Token counts stay zero rather than being estimated. A fabricated count
            # would flow into the cost report and make a free call look priced.
            usage=Usage(),
            latency_ms=int((time.perf_counter() - started) * 1000),
            estimated_cost_usd=Decimal(0),
        )

    async def embed(
        self,
        texts: Sequence[str],
        *,
        task_type: EmbeddingTaskType = EmbeddingTaskType.SEMANTIC_SIMILARITY,
    ) -> EmbeddingResult:
        started = time.perf_counter()
        vectors = [embed_text(text, self.embedding_dim) for text in texts]
        return EmbeddingResult(
            vectors=vectors,
            model=HEURISTIC_MODEL,
            dim=self.embedding_dim,
            task_type=task_type,
            usage=Usage(),
            latency_ms=int((time.perf_counter() - started) * 1000),
            estimated_cost_usd=Decimal(0),
        )

    async def close(self) -> None:
        return None
