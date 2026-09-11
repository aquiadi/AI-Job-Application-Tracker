"""What tailoring is allowed to return.

Every generated bullet carries `source_item_ids`. That is not metadata — it is the
thing the validator checks against, and a bullet that cites nothing is rejected before
it reaches a document. ADR 13.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

#: A tailored resume longer than this is not tailored.
MAX_BULLETS = 24
MAX_BULLET_CHARS = 400
#: Short enough to allow a terse line, long enough that a model cannot answer with a
#: fragment. A skills row is exempt by being assembled into one line rather than by
#: lowering this.
MIN_BULLET_CHARS = 10


class TailoredBullet(BaseModel):
    """One rewritten line, and the evidence it was built from."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=MIN_BULLET_CHARS, max_length=MAX_BULLET_CHARS)
    source_item_ids: list[uuid.UUID] = Field(
        min_length=1,
        description=(
            "The ids of the profile items this bullet was written from, copied exactly "
            "from the [id] markers in the evidence. At least one, and only ids that "
            "appear in the evidence."
        ),
    )


class TailoredSection(BaseModel):
    """A group of bullets under a heading the resume already had."""

    model_config = ConfigDict(frozen=True)

    heading: str = Field(min_length=2, max_length=120)
    organisation: str | None = Field(default=None, max_length=255)
    role: str | None = Field(default=None, max_length=255)
    bullets: list[TailoredBullet] = Field(default_factory=list)


class TailoredResume(BaseModel):
    """The structured document, before rendering and before validation."""

    model_config = ConfigDict(frozen=True)

    summary: str | None = Field(
        default=None,
        max_length=600,
        description="A short opening paragraph, or null. Subject to the same rules as a bullet.",
    )
    summary_source_item_ids: list[uuid.UUID] = Field(default_factory=list)
    sections: list[TailoredSection] = Field(default_factory=list)

    def bullets(self) -> list[TailoredBullet]:
        return [bullet for section in self.sections for bullet in section.bullets]


class CoverLetter(BaseModel):
    """A short letter, grounded the same way."""

    model_config = ConfigDict(frozen=True)

    paragraphs: list[TailoredBullet] = Field(
        default_factory=list,
        description="Each paragraph cites the profile items it draws on, as a bullet does.",
    )
