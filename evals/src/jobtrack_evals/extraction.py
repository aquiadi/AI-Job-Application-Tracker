"""Measuring extraction: Gemini against the rule-based baseline.

The design decision that makes this worth running is in ADR 10: there are two backends,
so every number has something to compare against. "Gemini scored 0.82 on requirement
recall" means very little. "Gemini scored 0.82 where a bullet-splitter scores 0.61"
says what the model is worth on this task, which is the question.

Four measurements, and the fourth is the one that matters most for this product:

* **Requirement recall and precision** against hand-labelled requirements, matched by
  token overlap rather than string equality, because a correct extraction may trim a
  bullet differently than a labeller did.
* **Must/nice accuracy** over the requirements that were found. Weighting the score
  depends on this being right.
* **Field accuracy** over the scalar fields.
* **Hallucinated-field rate** — a field filled in when the posting does not state it.
  This is the one that decides whether the output can be shown to a user without a
  warning attached, and a model that scores well on the other three and badly on this
  is a model that makes things up confidently.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobtrack_core.db.enums import SourceAts
from jobtrack_core.ingest.canonical import CanonicalPosting
from jobtrack_core.ingest.extraction import ExtractedPosting

#: Token overlap above which a predicted requirement counts as the labelled one. A
#: correct extraction may keep "5+ years of Python or Go" where the label says
#: "5+ years building backend services in Python or Go", and string equality would
#: score that as both a miss and a false positive.
MATCH_THRESHOLD = 0.5

#: Fields a posting may leave unstated. The hallucinated-field rate is measured over
#: exactly these, because filling one that the posting does not state is the failure.
NULLABLE_FIELDS = (
    "remote_policy",
    "employment_type",
    "seniority",
    "experience_years_min",
    "experience_years_max",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
)

_WORD = re.compile(r"[a-z0-9+#.]+")
# fmt: off
_STOPWORDS = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
    "in", "is", "it", "its", "of", "on", "or", "that", "the", "to", "with", "you",
    "your", "we", "our", "their", "this", "those", "these", "will",
])
# fmt: on


@dataclass(frozen=True, slots=True)
class LabelledPosting:
    """One hand-labelled posting from the dataset."""

    slug: str
    source_ats: SourceAts
    body: str
    title: str | None
    company: str | None
    expected: ExtractedPosting
    #: False when a human has not checked the labels. Excluded from reported numbers;
    #: a model-drafted label scored against a model is not a measurement.
    reviewed: bool

    def posting(self) -> CanonicalPosting:
        return CanonicalPosting(
            source_ats=self.source_ats,
            title=self.title,
            company=self.company,
            body=self.body,
        )


@dataclass(slots=True)
class Scores:
    """What one backend achieved over the dataset."""

    backend: str
    postings: int = 0
    schema_valid: int = 0
    requirements_expected: int = 0
    requirements_found: int = 0
    requirements_matched: int = 0
    kind_correct: int = 0
    fields_expected: int = 0
    fields_correct: int = 0
    #: Nullable fields the posting leaves unstated that the backend filled anyway.
    hallucinated: int = 0
    nullable_unstated: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return _ratio(self.requirements_matched, self.requirements_expected)

    @property
    def precision(self) -> float:
        return _ratio(self.requirements_matched, self.requirements_found)

    @property
    def f1(self) -> float:
        if not (self.recall and self.precision):
            return 0.0
        return 2 * self.recall * self.precision / (self.recall + self.precision)

    @property
    def kind_accuracy(self) -> float:
        return _ratio(self.kind_correct, self.requirements_matched)

    @property
    def field_accuracy(self) -> float:
        return _ratio(self.fields_correct, self.fields_expected)

    @property
    def hallucination_rate(self) -> float:
        return _ratio(self.hallucinated, self.nullable_unstated)

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "postings": self.postings,
            "schema_valid": self.schema_valid,
            "requirement_recall": round(self.recall, 4),
            "requirement_precision": round(self.precision, 4),
            "requirement_f1": round(self.f1, 4),
            "kind_accuracy": round(self.kind_accuracy, 4),
            "field_accuracy": round(self.field_accuracy, 4),
            "hallucinated_field_rate": round(self.hallucination_rate, 4),
            "failures": self.failures,
        }


def load_dataset(directory: Path) -> list[LabelledPosting]:
    """Read every labelled posting from a directory of JSON files."""
    postings: list[LabelledPosting] = []
    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        postings.append(
            LabelledPosting(
                slug=path.stem,
                source_ats=SourceAts(raw.get("source_ats", "pasted")),
                body=raw["body"],
                title=raw.get("title"),
                company=raw.get("company"),
                expected=ExtractedPosting.model_validate(raw["expected"]),
                reviewed=bool(raw.get("reviewed", False)),
            )
        )
    return postings


def score_one(expected: ExtractedPosting, actual: ExtractedPosting, into: Scores) -> None:
    """Accumulate one posting's result."""
    into.postings += 1
    into.schema_valid += 1

    remaining = list(actual.requirements)
    into.requirements_expected += len(expected.requirements)
    into.requirements_found += len(actual.requirements)

    for wanted in expected.requirements:
        best = _best_match(wanted.text, [item.text for item in remaining])
        if best is None:
            continue
        into.requirements_matched += 1
        found = remaining.pop(best)
        if found.kind is wanted.kind:
            into.kind_correct += 1

    for name in NULLABLE_FIELDS:
        want = getattr(expected, name)
        got = getattr(actual, name)
        if want is None:
            # The posting does not state it. Anything but null is invented.
            into.nullable_unstated += 1
            if got is not None:
                into.hallucinated += 1
            continue
        into.fields_expected += 1
        if _same(want, got):
            into.fields_correct += 1


def _best_match(wanted: str, candidates: list[str]) -> int | None:
    wanted_tokens = _tokens(wanted)
    if not wanted_tokens:
        return None

    best_index: int | None = None
    best_score = MATCH_THRESHOLD
    for index, candidate in enumerate(candidates):
        tokens = _tokens(candidate)
        if not tokens:
            continue
        overlap = len(wanted_tokens & tokens) / len(wanted_tokens | tokens)
        if overlap >= best_score:
            best_index, best_score = index, overlap
    return best_index


def _tokens(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS}


def _same(want: object, got: object) -> bool:
    if want is None or got is None:
        return want is got
    return str(want).strip().lower() == str(got).strip().lower()


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0
