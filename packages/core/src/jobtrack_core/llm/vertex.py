"""The real client. `google-genai` against Vertex AI.

Three things here are not obvious from the SDK surface.

**Two clients, not one.** Generation and embedding are served from different Vertex
locations and a single client cannot reach both: Gemini 3.x generative models are not
served from ordinary regions, and `gemini-embedding-001` is not served from `global`.
A single `location` produces a 404 on whichever call it is wrong for. ADR 2 records how
this was discovered.

**A structured response can still be invalid.** `response_schema` constrains decoding,
not semantics, and a response can arrive truncated by `max_output_tokens`, empty
because of a safety stop, or with a field the schema allows and the validators reject.
So the answer is re-parsed with the same Pydantic model, and a failure is retried once
with the validation errors in the prompt.

**Thinking is off for structured extraction.** Gemini 3.x bills thinking tokens as
output. Extraction is a schema-constrained transcription task where the thinking budget
buys very little, so it is set to MINIMAL rather than left on its default.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from jobtrack_core.config.settings import Settings
from jobtrack_core.llm.client import (
    EmbeddingResult,
    EmbeddingTaskType,
    InvalidOutputError,
    LlmError,
    LlmResult,
    Usage,
)
from jobtrack_core.llm.pricing import estimate_cost_usd
from jobtrack_core.llm.prompts import RenderedPrompt

#: Vertex rejects an embedding request whose batch is larger than this.
EMBED_BATCH_LIMIT = 250

_RETRY_PREAMBLE = (
    "Your previous answer did not validate against the required schema.\n"
    "Errors:\n{errors}\n\n"
    "Answer the original request again, correcting exactly those problems. "
    "Do not add commentary.\n\n"
)


@dataclass(slots=True)
class VertexLlmClient:
    """Structured generation and embedding against Vertex AI."""

    settings: Settings
    _generate: genai.Client
    _embed: genai.Client

    @classmethod
    def create(cls, settings: Settings) -> VertexLlmClient:
        if not settings.project_id:
            raise LlmError("GOOGLE_CLOUD_PROJECT is required to call Vertex AI")
        http = types.HttpOptions(timeout=int(settings.vertex.request_timeout_seconds * 1000))
        return cls(
            settings=settings,
            _generate=genai.Client(
                vertexai=True,
                project=settings.project_id,
                location=settings.vertex.location,
                http_options=http,
            ),
            _embed=genai.Client(
                vertexai=True,
                project=settings.project_id,
                location=settings.vertex.embedding_region,
                http_options=http,
            ),
        )

    @property
    def generate_model(self) -> str:
        return self.settings.vertex.generate_model

    @property
    def embedding_model(self) -> str:
        return self.settings.vertex.embedding_model

    async def generate_structured[T: BaseModel](
        self,
        prompt: RenderedPrompt,
        schema: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> LlmResult[T]:
        chosen = model or self.settings.vertex.generate_model
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=temperature,
            max_output_tokens=self.settings.vertex.max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
        )

        started = time.perf_counter()
        total = Usage()
        errors: list[dict[str, Any]] = []
        text = prompt.text

        for attempt in (1, 2):
            response = await self._call(chosen, text, config)
            total = total + _usage_of(response)
            try:
                value = _parse(response, schema)
            except InvalidOutputError as exc:
                errors = exc.errors
                if attempt == 2:
                    raise InvalidOutputError(
                        f"{prompt.prompt_id}.{prompt.version} failed validation twice", errors
                    ) from exc
                # The model is shown which fields failed, never the text that failed,
                # which keeps the retry free of anything a log would have to redact.
                text = _RETRY_PREAMBLE.format(errors=json.dumps(errors, indent=2)) + prompt.text
                continue

            return LlmResult(
                value=value,
                model=chosen,
                prompt_id=prompt.prompt_id,
                prompt_version=prompt.version,
                usage=total,
                latency_ms=_elapsed_ms(started),
                estimated_cost_usd=estimate_cost_usd(
                    chosen,
                    input_tokens=total.input_tokens,
                    output_tokens=total.output_tokens,
                    cached_input_tokens=total.cached_input_tokens,
                    regional=self.settings.vertex.location not in {"global"},
                ),
                attempts=attempt,
                validation_errors=errors,
            )

        raise LlmError("unreachable")

    async def _call(
        self, model: str, text: str, config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        try:
            return await self._generate.aio.models.generate_content(
                model=model, contents=text, config=config
            )
        # The SDK raises across a wide surface — transport, auth, quota and API
        # errors are all distinct exception types — and every one of them means
        # the same thing to the caller: the call did not produce an answer.
        except Exception as exc:
            raise LlmError(f"Vertex generate_content failed: {type(exc).__name__}") from exc

    async def embed(
        self,
        texts: Sequence[str],
        *,
        task_type: EmbeddingTaskType = EmbeddingTaskType.SEMANTIC_SIMILARITY,
    ) -> EmbeddingResult:
        dim = self.settings.vertex.embedding_dim
        model = self.settings.vertex.embedding_model
        config = types.EmbedContentConfig(task_type=task_type.value, output_dimensionality=dim)

        started = time.perf_counter()
        vectors: list[list[float]] = []
        total = Usage()

        for offset in range(0, len(texts), EMBED_BATCH_LIMIT):
            batch = list(texts[offset : offset + EMBED_BATCH_LIMIT])
            try:
                response = await self._embed.aio.models.embed_content(
                    model=model, contents=batch, config=config
                )
            except Exception as exc:  # See the note in _call.
                raise LlmError(f"Vertex embed_content failed: {type(exc).__name__}") from exc

            returned = response.embeddings or []
            if len(returned) != len(batch):
                raise LlmError(f"asked for {len(batch)} embeddings, got {len(returned)}")
            for embedding in returned:
                if not embedding.values:
                    raise LlmError("Vertex returned an embedding with no values")
                vectors.append(list(embedding.values))

            if response.metadata and response.metadata.billable_character_count:
                # The embedding model bills characters, not tokens. Recording them in
                # the token field would make the cost estimate silently wrong, so the
                # count is converted at the model's documented 4 characters per token.
                total = total + Usage(input_tokens=response.metadata.billable_character_count // 4)

        return EmbeddingResult(
            vectors=vectors,
            model=model,
            dim=dim,
            task_type=task_type,
            usage=total,
            latency_ms=_elapsed_ms(started),
            estimated_cost_usd=estimate_cost_usd(
                model, input_tokens=total.input_tokens, output_tokens=0
            ),
        )

    async def close(self) -> None:
        # The SDK's async transport is closed when the client is collected; there is no
        # public aclose() on genai.Client as of 2.23.
        return None


def _parse[T: BaseModel](response: types.GenerateContentResponse, schema: type[T]) -> T:
    """Re-validate the model's answer with the same schema that constrained it."""
    text = response.text
    if not text:
        # An empty answer with a finish reason is the shape of a truncation or a safety
        # stop. Both are invalid output rather than transport failures, so both take
        # the retry path.
        reason = response.candidates[0].finish_reason if response.candidates else None
        raise InvalidOutputError(
            "the model returned no text",
            [{"type": "empty_response", "finish_reason": str(reason)}],
        )
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise InvalidOutputError("the model's answer did not validate", _redact(exc)) from exc


def _redact(exc: ValidationError) -> list[dict[str, Any]]:
    """Keep which field failed and why; drop the value that failed.

    `ValidationError.errors()` includes the offending input under `input`, which for
    this system can be posting or resume text. `llm_calls.validation_errors` is stored
    and logged, so the value cannot travel with the error.
    """
    return [
        {"loc": list(error["loc"]), "type": error["type"], "msg": error["msg"]}
        for error in exc.errors(include_url=False)
    ]


def _usage_of(response: types.GenerateContentResponse) -> Usage:
    meta = response.usage_metadata
    if meta is None:
        return Usage()
    # Thinking tokens are billed as output and are absent from candidates_token_count.
    output = (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)
    return Usage(
        input_tokens=meta.prompt_token_count or 0,
        output_tokens=output,
        cached_input_tokens=meta.cached_content_token_count or 0,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
