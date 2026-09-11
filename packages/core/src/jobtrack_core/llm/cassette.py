"""Recorded model responses, replayed by content hash.

This is what unit tests use. It exists so that a test asserting "extraction turns this
posting into these six requirements" runs with no credentials, no network and no
nondeterminism, while still exercising the real parsing, validation and retry paths —
the cassette holds what Vertex actually returned, so a schema change that breaks
parsing breaks the test.

The key is `(prompt_id, prompt_version, sha256 of the rendered prompt)`. Including the
version means a prompt revision does not silently reuse the previous version's
recordings; it means the recordings are missing, which is the correct signal.

A miss raises. A cassette client that fell through to a live call would make the test
suite's credential requirement depend on which cassettes happened to exist.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from jobtrack_core.llm.client import (
    EmbeddingResult,
    EmbeddingTaskType,
    InvalidOutputError,
    LlmError,
    LlmResult,
    Usage,
)
from jobtrack_core.llm.prompts import RenderedPrompt

CASSETTE_MODEL = "cassette"


class CassetteMissError(LlmError):
    """No recording for this call.

    The message names the key and the file that would hold it, because the fix is
    always either to record it or to notice that the input changed when it should not
    have.
    """


@dataclass(frozen=True, slots=True)
class Cassette:
    """One recorded exchange."""

    key: str
    model: str
    response_json: str
    usage: Usage
    latency_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "model": self.model,
            "response_json": self.response_json,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "cached_input_tokens": self.usage.cached_input_tokens,
            },
            "latency_ms": self.latency_ms,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Cassette:
        usage = raw.get("usage") or {}
        return cls(
            key=str(raw["key"]),
            model=str(raw["model"]),
            response_json=str(raw["response_json"]),
            usage=Usage(
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                cached_input_tokens=int(usage.get("cached_input_tokens", 0)),
            ),
            latency_ms=int(raw.get("latency_ms", 0)),
        )


def cassette_key(prompt: RenderedPrompt) -> str:
    """`<prompt_id>.<version>.<first 16 hex of sha256(text)>`.

    Truncated because the full digest makes a filename that no one can compare by eye,
    and 64 bits is far beyond collision range for a fixture set of this size.
    """
    digest = hashlib.sha256(prompt.text.encode("utf-8")).hexdigest()[:16]
    return f"{prompt.prompt_id}.{prompt.version}.{digest}"


@dataclass(slots=True)
class CassetteLlmClient:
    """Replays recorded generations; embeds locally.

    Embeddings are not recorded. A cassette per embedded string would be thousands of
    files that say nothing a test asserts on, so embedding falls through to the same
    deterministic hashing the heuristic backend uses — which is a real function of its
    input, so a test can still assert that two similar texts score above two unrelated
    ones.
    """

    directory: Path
    embedding_dim: int = 768

    @property
    def generate_model(self) -> str:
        return CASSETTE_MODEL

    @property
    def embedding_model(self) -> str:
        return CASSETTE_MODEL

    async def generate_structured[T: BaseModel](
        self,
        prompt: RenderedPrompt,
        schema: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> LlmResult[T]:
        key = cassette_key(prompt)
        path = self.directory / f"{key}.json"
        if not path.is_file():
            raise CassetteMissError(f"no cassette {key}; expected it at {path}")

        recorded = Cassette.from_dict(json.loads(path.read_text(encoding="utf-8")))
        started = time.perf_counter()
        try:
            value = schema.model_validate_json(recorded.response_json)
        except ValidationError as exc:
            # The recording no longer satisfies the schema, which means the schema
            # changed. That is a real finding, not a test-harness problem.
            raise InvalidOutputError(
                f"cassette {key} no longer validates against {schema.__name__}",
                [{"loc": list(e["loc"]), "type": e["type"], "msg": e["msg"]} for e in exc.errors()],
            ) from exc

        return LlmResult(
            value=value,
            model=recorded.model,
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            usage=recorded.usage,
            latency_ms=int((time.perf_counter() - started) * 1000),
            estimated_cost_usd=Decimal(0),
        )

    async def embed(
        self,
        texts: Sequence[str],
        *,
        task_type: EmbeddingTaskType = EmbeddingTaskType.SEMANTIC_SIMILARITY,
    ) -> EmbeddingResult:
        from jobtrack_core.llm.heuristic.embed import embed_text

        started = time.perf_counter()
        return EmbeddingResult(
            vectors=[embed_text(text, self.embedding_dim) for text in texts],
            model=CASSETTE_MODEL,
            dim=self.embedding_dim,
            task_type=task_type,
            usage=Usage(),
            latency_ms=int((time.perf_counter() - started) * 1000),
            estimated_cost_usd=Decimal(0),
        )

    async def close(self) -> None:
        return None

    def record(self, prompt: RenderedPrompt, result: LlmResult[BaseModel]) -> Path:
        """Write a cassette for a live result. Used by the recording script, not by tests."""
        key = cassette_key(prompt)
        path = self.directory / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        cassette = Cassette(
            key=key,
            model=result.model,
            response_json=result.value.model_dump_json(),
            usage=result.usage,
            latency_ms=result.latency_ms,
        )
        path.write_text(json.dumps(cassette.to_dict(), indent=2) + "\n", encoding="utf-8")
        return path
