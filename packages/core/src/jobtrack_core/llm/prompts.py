"""Prompt loading, versioning and rendering.

Prompts are files, not string literals in Python. A prompt is the part of this system
most likely to change without a code review, and keeping it in `prompts/` means a diff
shows the wording change on its own rather than buried in a handler.

The filename carries the identity: `<prompt_id>.v<N>.md`. Changing a prompt means
adding a file, never editing one in place, because `llm_calls.prompt_version` is how a
cost or quality regression gets attributed later. A cassette recorded against v1 stays
valid for v1 forever.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from types import MappingProxyType

#: `prompts/` at the repository root, found by walking up from this file. Prompts are
#: package data in a deployed image and a sibling directory in a checkout, so the
#: search covers both rather than assuming one.
_SEARCH_ROOTS = ("prompts",)

_FILENAME = re.compile(r"^(?P<prompt_id>[a-z0-9_]+)\.v(?P<version>\d+)\.md$")
_PLACEHOLDER = re.compile(r"\{\{\s*(?P<name>[a-z0-9_]+)\s*\}\}")


class PromptError(Exception):
    """A prompt could not be found, or was rendered with the wrong variables."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """A template and the identity that goes into `llm_calls`."""

    prompt_id: str
    version: str
    template: str

    @property
    def variables(self) -> frozenset[str]:
        return frozenset(match["name"] for match in _PLACEHOLDER.finditer(self.template))

    def render(self, **values: str) -> RenderedPrompt:
        """Substitute every placeholder, refusing partial renders.

        Both directions are errors. A missing value would send `{{ posting }}` to the
        model as literal text, and an unexpected one means the caller believes it is
        filling a slot that the prompt does not have — usually because the prompt was
        revised and the caller was not.
        """
        expected = self.variables
        provided = frozenset(values)
        if missing := expected - provided:
            raise PromptError(f"{self.prompt_id}.v{self.version} needs {sorted(missing)}")
        if extra := provided - expected:
            raise PromptError(f"{self.prompt_id}.v{self.version} has no slot for {sorted(extra)}")

        rendered = _PLACEHOLDER.sub(lambda m: values[m["name"]], self.template)
        return RenderedPrompt(
            prompt_id=self.prompt_id,
            version=self.version,
            text=rendered,
            values=MappingProxyType(dict(values)),
        )


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """A prompt with its slots filled, ready to send.

    `values` is kept alongside the rendered text because not every backend wants the
    rendered form. The heuristic backend needs the posting, not a posting wrapped in
    instructions addressed to a model, and re-extracting it from `text` by pattern
    would couple that backend to the prompt's wording.
    """

    prompt_id: str
    version: str
    text: str
    values: Mapping[str, str] = field(default_factory=dict)


def prompts_dir() -> Path:
    """Locate `prompts/` by walking up from this module."""
    for parent in Path(__file__).resolve().parents:
        for name in _SEARCH_ROOTS:
            candidate = parent / name
            if candidate.is_dir():
                return candidate
    raise PromptError("no prompts/ directory found above " + str(Path(__file__).resolve()))


@cache
def load_prompt(prompt_id: str, version: int | None = None) -> Prompt:
    """Load a prompt, defaulting to its highest version.

    Callers pin a version when they need one; leaving it off picks the newest, which is
    what a prompt author wants after adding a file.
    """
    directory = prompts_dir()
    available: dict[int, Path] = {}
    for path in directory.glob(f"{prompt_id}.v*.md"):
        if match := _FILENAME.match(path.name):
            available[int(match["version"])] = path

    if not available:
        raise PromptError(f"no prompt {prompt_id!r} in {directory}")

    chosen = max(available) if version is None else version
    if chosen not in available:
        raise PromptError(f"{prompt_id} has no v{chosen}; found {sorted(available)}")

    return Prompt(
        prompt_id=prompt_id,
        version=f"v{chosen}",
        template=available[chosen].read_text(encoding="utf-8").strip(),
    )
