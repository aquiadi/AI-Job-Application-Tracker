"""The contract every model call goes through.

One protocol, three implementations, and nothing outside this package constructs a
`genai.Client`. ADR 10 records why.

The return type carries accounting, not just the answer. `llm_calls` needs token
counts, latency, cost and outcome for every call, and a client that returned only the
parsed value would push that bookkeeping onto each caller — which is where it gets
forgotten.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel

from jobtrack_core.llm.prompts import RenderedPrompt


class EmbeddingTaskType(enum.StrEnum):
    """How the embedding will be used, which changes the vector the model returns.

    `SEMANTIC_SIMILARITY` is the default for this system: a requirement and a profile
    item are the same genre of text, two declarative statements about capability, which
    argues for a symmetric space over the query-to-passage asymmetry of `RETRIEVAL_*`.
    ADR 4 records that this is an argument rather than a measurement, and that M3's
    calibration decides it.
    """

    SEMANTIC_SIMILARITY = "SEMANTIC_SIMILARITY"
    RETRIEVAL_DOCUMENT = "RETRIEVAL_DOCUMENT"
    RETRIEVAL_QUERY = "RETRIEVAL_QUERY"


class LlmError(Exception):
    """A model call failed in a way the caller has to handle."""


class InvalidOutputError(LlmError):
    """The model answered, and the answer did not satisfy the schema.

    Carries the validation errors so the retry can put them in front of the model, and
    so `llm_calls.validation_errors` records which fields failed without recording the
    text that failed.
    """

    def __init__(self, message: str, errors: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.errors = errors


@dataclass(frozen=True, slots=True)
class Usage:
    """Tokens, as the provider counted them."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
        )


@dataclass(frozen=True, slots=True)
class LlmResult[T]:
    """A parsed answer and everything `llm_calls` needs to record about getting it."""

    value: T
    model: str
    prompt_id: str
    prompt_version: str
    usage: Usage
    latency_ms: int
    estimated_cost_usd: Decimal
    #: 1 on first-try success, 2 when the schema retry was needed. Recorded because a
    #: rising retry rate is the earliest signal that a prompt and a schema have drifted.
    attempts: int = 1
    validation_errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """Vectors and the identity of the space they belong to.

    The model, width and task type travel with the vectors because comparing across
    spaces is meaningless but not erroneous: it returns a number, and the number is
    noise. Every vector row in the schema stores all three for the same reason.
    """

    vectors: list[list[float]]
    model: str
    dim: int
    task_type: EmbeddingTaskType
    usage: Usage
    latency_ms: int
    estimated_cost_usd: Decimal


class LlmClient(Protocol):
    """Structured generation and embedding. The only model interface in this system."""

    @property
    def generate_model(self) -> str: ...

    @property
    def embedding_model(self) -> str: ...

    async def generate_structured[T: BaseModel](
        self,
        prompt: RenderedPrompt,
        schema: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> LlmResult[T]:
        """Generate a value of `schema`, retrying once with the validation error.

        Raises :class:`InvalidOutputError` if the second attempt also fails, so the
        caller can dead-letter the work rather than storing a half-parsed result.
        """
        ...

    async def embed(
        self,
        texts: Sequence[str],
        *,
        task_type: EmbeddingTaskType = EmbeddingTaskType.SEMANTIC_SIMILARITY,
    ) -> EmbeddingResult:
        """Embed each text, preserving order."""
        ...

    async def close(self) -> None:
        """Release any transport the client owns."""
        ...
