"""The schema extraction has to produce, and the rules that make it checkable.

This model is the `response_schema` sent to Gemini and the validator its answer is
re-parsed with. Those being the same object is the point: a field the model may not
invent is a field this model rejects.

Two properties matter more than the field list.

**Absence is representable.** Every posting-derived field is optional and defaults to
null. A model asked for `salary_min` with nowhere to say "not stated" will state
something, and the extraction eval measures exactly that as the hallucinated-field
rate.

**Requirements are quotes, not summaries.** `text` is meant to be the posting's own
wording. That is what makes a requirement comparable to a profile item by embedding,
and what lets the interface show the user the line the judgement came from.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jobtrack_core.db.enums import (
    EmploymentType,
    RemotePolicy,
    RequirementKind,
    SalaryPeriod,
    Seniority,
)

#: A posting with more distinct requirements than this is almost always a page that
#: also contains unrelated listings. Truncating beats embedding the whole careers site.
MAX_REQUIREMENTS = 40
#: Long enough for a compound requirement, short enough to reject a whole paragraph
#: arriving as one "requirement".
MAX_REQUIREMENT_CHARS = 400


class ExtractedRequirement(BaseModel):
    """One requirement, in the posting's own words."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=3, max_length=MAX_REQUIREMENT_CHARS)
    kind: RequirementKind = Field(
        description=(
            "must if the posting presents it as required, expected or a minimum; "
            "nice if it is a bonus, a plus, preferred, or desirable"
        )
    )


class ExtractedPosting(BaseModel):
    """Everything extraction is allowed to return.

    Field descriptions are part of the contract, not documentation: they are serialised
    into the `response_schema` Vertex receives, so they are the instructions the model
    actually reads for each field.
    """

    model_config = ConfigDict(frozen=True)

    title: str | None = Field(default=None, description="The role title, exactly as written")
    company: str | None = Field(default=None, description="The hiring company, not the ATS vendor")
    location: str | None = Field(default=None, description="Primary location, null if unstated")

    remote_policy: RemotePolicy | None = Field(
        default=None, description="Only if the posting says so. Null if it does not."
    )
    employment_type: EmploymentType | None = None
    seniority: Seniority | None = Field(
        default=None, description="Inferred from the title only, null if the title is ambiguous"
    )

    experience_years_min: int | None = Field(default=None, ge=0, le=50)
    experience_years_max: int | None = Field(default=None, ge=0, le=50)

    salary_min: Decimal | None = Field(default=None, ge=0)
    salary_max: Decimal | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(
        default=None, description="ISO 4217 code such as USD, null unless the posting states one"
    )
    salary_period: SalaryPeriod | None = None

    requirements: list[ExtractedRequirement] = Field(
        default_factory=list,
        description=(
            "Each distinct requirement as its own entry, quoting the posting rather "
            "than summarising it. Do not merge two requirements into one entry."
        ),
    )
    hard_skills: list[str] = Field(
        default_factory=list,
        description="Named technologies, tools and languages the posting mentions",
    )
    soft_skills: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)

    @field_validator("requirements", "hard_skills", "soft_skills", "responsibilities", mode="after")
    @classmethod
    def _cap_length(cls, value: list[object]) -> list[object]:
        return value[:MAX_REQUIREMENTS]

    @field_validator("salary_currency", mode="after")
    @classmethod
    def _upper_currency(cls, value: str | None) -> str | None:
        # The column is CHAR(3) and the posting might say "usd". Anything that is not
        # a three-letter code is dropped rather than stored: a currency this cannot
        # parse is worse than no currency, because it renders on screen.
        if value is None:
            return None
        code = value.strip().upper()
        return code if len(code) == 3 and code.isalpha() else None

    def ordered_requirements(self) -> list[ExtractedRequirement]:
        """Must-haves first, original order preserved inside each group.

        The board and the score both read must-haves first, and doing the sort once
        here keeps `display_order` meaningful on the way into the database.
        """
        musts = [r for r in self.requirements if r.kind is RequirementKind.MUST]
        nices = [r for r in self.requirements if r.kind is RequirementKind.NICE]
        return musts + nices
