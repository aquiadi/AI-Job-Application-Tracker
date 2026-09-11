"""What a parsed resume is allowed to contain.

The contact block is conspicuously absent from everything here, and that is the whole
design. Name, email, phone, address and links live on `profiles` as ordinary columns,
are never included in prompt context, and are re-attached when a document is rendered.
A parsed resume therefore has nowhere to put them, which is a stronger guarantee than
remembering to strip them: there is no field to leak.

`ParsedResume` is the `response_schema` for the import prompt and the validator its
answer is re-parsed with, for the same reason `ExtractedPosting` is both.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jobtrack_core.db.enums import ProfileItemKind

#: A resume with more items than this is a parse that ran away, usually on a document
#: whose text layer interleaves two columns.
MAX_ITEMS = 200
MAX_ITEM_CHARS = 600


#: A skill can be one character. "R", "Go", "C#" and "ML" are all real, and a blanket
#: three-character minimum silently drops the most specific tokens a posting screens on.
MIN_SKILL_CHARS = 1
#: A bullet, project or qualification of one or two characters is a parsing artefact.
MIN_PROSE_CHARS = 3


class ParsedItem(BaseModel):
    """One bullet, project, skill or qualification.

    Granularity is the product decision, not a storage one. A whole resume as one
    vector answers "is this person roughly like this posting". A bullet on its own
    answers "which specific thing I have done covers this specific requirement", which
    is what the score needs and what a citation has to be able to point at.
    """

    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=MIN_SKILL_CHARS, max_length=MAX_ITEM_CHARS)
    kind: ProfileItemKind = Field(
        description=(
            "experience_bullet for something done in a role, project for personal or "
            "side work, skill for a named technology, education for a degree or "
            "certification"
        )
    )
    organisation: str | None = Field(
        default=None, max_length=255, description="Employer or institution, null if unclear"
    )
    role: str | None = Field(default=None, max_length=255, description="Job title held")
    started_on: date | None = Field(
        default=None, description="First day of the month if only a month is given"
    )
    ended_on: date | None = Field(default=None, description="Null if this is current")

    @model_validator(mode="after")
    def _prose_is_long_enough(self) -> ParsedItem:
        """Only skills may be very short.

        Enforced here rather than by a single field minimum, because the two cases
        genuinely differ: "Go" is a skill and "Go" is not an experience bullet.
        """
        if self.kind is not ProfileItemKind.SKILL and len(self.text.strip()) < MIN_PROSE_CHARS:
            raise ValueError(f"a {self.kind.value} needs at least {MIN_PROSE_CHARS} characters")
        return self


class ParsedResume(BaseModel):
    """Everything the import is allowed to return."""

    model_config = ConfigDict(frozen=True)

    headline: str | None = Field(
        default=None, max_length=255, description="A one-line professional summary if stated"
    )
    items: list[ParsedItem] = Field(default_factory=list)

    @field_validator("items", mode="after")
    @classmethod
    def _cap(cls, value: list[ParsedItem]) -> list[ParsedItem]:
        return value[:MAX_ITEMS]


class NudgeDraft(BaseModel):
    """A drafted follow-up email. Never sent by this system.

    No recipient field, and no signature. The user sends it themselves from their own
    client, which is what "nothing is sent on your behalf" means in practice rather
    than as a promise.
    """

    model_config = ConfigDict(frozen=True)

    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=20, max_length=2000)
