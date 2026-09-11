"""Rule-based requirement extraction. The baseline the model has to beat.

ADR 10 explains why this exists rather than a stub. Two reasons, and the second is the
one that justifies the code: the product has to work without credentials, and an eval
reporting "Gemini scored 0.82" says nothing without a floor to compare against.

The approach is ordinary information extraction, and it works because job postings are
written to a convention. Requirements appear as bullets under a heading that names
them, and the heading says whether they are required or preferred. Where the heading is
absent, modal phrasing carries the same signal: "must have" and "5+ years" are
requirements, "nice to have" and "bonus" are not.

What it cannot do is the thing the model is for: a requirement written as prose in the
middle of a paragraph is invisible here, and one bullet holding two requirements stays
one. The eval measures exactly that gap.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from jobtrack_core.db.enums import RequirementKind
from jobtrack_core.ingest.extraction import (
    MAX_REQUIREMENT_CHARS,
    ExtractedPosting,
    ExtractedRequirement,
)

_BULLET = re.compile(r"^\s*[-*]\s+(?P<text>.+)$")
#: A heading is a short line, optionally ending in a colon, with no terminal full stop.
_HEADING = re.compile(r"^\s*(?P<text>[A-Z][^.!?]{2,60}?):?\s*$")

# Every alternative here is a stem followed by `\w*`, not a word followed by `\b`.
# A trailing `\b` after "requirement" cannot match "Requirements", because the `s` is
# itself a word character — which silently classified every such heading as unknown.
_MUST_HEADINGS = re.compile(
    r"\b(requirement\w*|qualification\w*|must[- ]have\w*|what you.{0,5}ll need"
    r"|what we.{0,5}re looking|who you are|skill\w*|experience|you have|minimum)",
    re.IGNORECASE,
)
_NICE_HEADINGS = re.compile(
    r"\b(nice[- ]to[- ]have\w*|bonus\w*|preferred|plus\w*|desirable|ideally"
    r"|good to have|extra credit)",
    re.IGNORECASE,
)
_RESPONSIBILITY_HEADINGS = re.compile(
    r"\b(responsibilit\w*|what you.{0,5}ll do|the role|day[- ]to[- ]day|your impact"
    r"|about the (?:role|job)|duties)",
    re.IGNORECASE,
)
#: Headings that introduce neither. Bullets under these are dropped.
_NOISE_HEADINGS = re.compile(
    r"\b(benefit\w*|perk\w*|compensation|salary|equal opportunit\w*"
    r"|about (?:us|the company)|our (?:values|mission)|why join|how to apply"
    r"|interview process|eeo)",
    re.IGNORECASE,
)

_NICE_INLINE = re.compile(
    r"\b(nice to have|bonus|a plus|preferred|ideally|desirable|familiarity with"
    r"|exposure to|would be great)\b",
    re.IGNORECASE,
)
_MUST_INLINE = re.compile(
    r"\b(must|required|minimum|at least|proven|strong|solid|demonstrated|\d+\+?\s*years?)\b",
    re.IGNORECASE,
)

# Both dash characters are matched literally: postings write experience ranges with
# an en dash at least as often as with a hyphen.
_YEARS = re.compile(
    r"(?P<min>\d{1,2})\s*(?:\+|or more|and above)?\s*"
    r"(?:-|–|to)?\s*(?P<max>\d{1,2})?\s*\+?\s*years?",  # noqa: RUF001
    re.IGNORECASE,
)
_SALARY = re.compile(
    r"(?P<currency>[$£€]|USD|EUR|GBP|INR)\s?(?P<amount>\d{1,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?[kK]\b)",
)
_CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR"}

#: Matched as whole words against the posting, and re-used by the grounding
#: validator so that "names a technology" means the same thing in both places.
#: Deliberately a fixed vocabulary: a
#: baseline that discovered new skills would be a worse baseline, because its behaviour
#: would change with its input rather than staying a constant to measure against.
_SKILLS: tuple[str, ...] = (
    "python",
    "go",
    "golang",
    "java",
    "javascript",
    "typescript",
    "rust",
    "ruby",
    "c++",
    "c#",
    "scala",
    "kotlin",
    "swift",
    "php",
    "sql",
    "bash",
    "r",
    "react",
    "next.js",
    "vue",
    "angular",
    "svelte",
    "node.js",
    "django",
    "flask",
    "fastapi",
    "rails",
    "spring",
    "express",
    ".net",
    "postgres",
    "postgresql",
    "mysql",
    "mongodb",
    "redis",
    "elasticsearch",
    "cassandra",
    "dynamodb",
    "snowflake",
    "bigquery",
    "clickhouse",
    "kafka",
    "rabbitmq",
    "pubsub",
    "aws",
    "gcp",
    "google cloud",
    "azure",
    "kubernetes",
    "docker",
    "terraform",
    "ansible",
    "jenkins",
    "github actions",
    "circleci",
    "argocd",
    "helm",
    "pytorch",
    "tensorflow",
    "jax",
    "scikit-learn",
    "pandas",
    "numpy",
    "spark",
    "airflow",
    "dbt",
    "kubeflow",
    "mlflow",
    "langchain",
    "hugging face",
    "graphql",
    "grpc",
    "rest",
    "microservices",
    "ci/cd",
    "linux",
    "git",
)
SKILL_VOCABULARY = tuple(
    (skill, re.compile(rf"(?<![\w.]){re.escape(skill)}(?![\w.])", re.IGNORECASE))
    for skill in _SKILLS
)

#: The model id recorded in `llm_calls` for anything this module produces, so a row
#: never claims a Gemini model wrote it.
HEURISTIC_MODEL = "heuristic"


def extract(body: str, *, title: str | None = None, company: str | None = None) -> ExtractedPosting:
    """Segment a posting into requirements without calling a model."""
    requirements: list[ExtractedRequirement] = []
    responsibilities: list[str] = []
    section: str | None = None

    for line in body.split("\n"):
        if not (stripped := line.strip()):
            continue

        bullet = _BULLET.match(line)
        if bullet is None:
            if heading := _HEADING.match(stripped):
                section = _classify_heading(heading["text"])
            continue

        text = bullet["text"].strip()[:MAX_REQUIREMENT_CHARS]
        if len(text) < 3:
            continue

        if section == "responsibility":
            responsibilities.append(text)
        elif section == "noise":
            continue
        elif section == "nice":
            requirements.append(ExtractedRequirement(text=text, kind=RequirementKind.NICE))
        elif section == "must":
            sectioned = _inline_kind(text, RequirementKind.MUST)
            requirements.append(ExtractedRequirement(text=text, kind=sectioned))
        elif inferred := _unsectioned_kind(text):
            requirements.append(ExtractedRequirement(text=text, kind=inferred))

    minimum, maximum = _years(body)
    salary_min, salary_max, currency = _salary(body)

    return ExtractedPosting(
        title=title,
        company=company,
        experience_years_min=minimum,
        experience_years_max=maximum,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=currency,
        requirements=requirements,
        hard_skills=_skills(body),
        responsibilities=responsibilities,
    )


def _classify_heading(text: str) -> str | None:
    # Order matters: "Nice to have" often follows "Requirements" and both patterns can
    # match a compound heading such as "Requirements and nice-to-haves".
    if _NICE_HEADINGS.search(text):
        return "nice"
    if _RESPONSIBILITY_HEADINGS.search(text):
        return "responsibility"
    if _NOISE_HEADINGS.search(text):
        return "noise"
    if _MUST_HEADINGS.search(text):
        return "must"
    return None


def _inline_kind(text: str, default: RequirementKind) -> RequirementKind:
    """A bullet under a must-heading can still mark itself optional."""
    return RequirementKind.NICE if _NICE_INLINE.search(text) else default


def _unsectioned_kind(text: str) -> RequirementKind | None:
    """Bullets with no heading above them, classified on phrasing alone."""
    if _NICE_INLINE.search(text):
        return RequirementKind.NICE
    if _MUST_INLINE.search(text):
        return RequirementKind.MUST
    return None


def _years(body: str) -> tuple[int | None, int | None]:
    """The smallest stated experience requirement, which is the one that gates."""
    best: tuple[int, int | None] | None = None
    for match in _YEARS.finditer(body):
        low = int(match["min"])
        high = int(match["max"]) if match["max"] else None
        if low > 50 or (high is not None and (high > 50 or high < low)):
            continue
        if best is None or low < best[0]:
            best = (low, high)
    return best if best else (None, None)


def _salary(body: str) -> tuple[Decimal | None, Decimal | None, str | None]:
    amounts: list[Decimal] = []
    currency: str | None = None
    for match in _SALARY.finditer(body):
        raw = match["amount"].replace(",", "")
        try:
            value = Decimal(raw[:-1]) * 1000 if raw[-1] in "kK" else Decimal(raw)
        except InvalidOperation:
            continue
        # Below this it is a fee, a discount or a page number, not a salary.
        if value < 1000:
            continue
        amounts.append(value)
        currency = currency or _CURRENCY_SYMBOLS.get(match["currency"], match["currency"].upper())

    if not amounts:
        return None, None, None
    low, high = min(amounts), max(amounts)
    return low, (high if high != low else None), currency


def _skills(body: str) -> list[str]:
    return [skill for skill, pattern in SKILL_VOCABULARY if pattern.search(body)]
