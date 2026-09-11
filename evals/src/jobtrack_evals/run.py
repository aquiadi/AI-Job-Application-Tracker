"""`make eval`. Runs the dataset through every available backend and writes a report.

The heuristic backend always runs, because it needs nothing. Vertex runs when
credentials exist. So this produces a useful report today — the baseline, and the
dataset's own shape — and the comparison that matters the moment there is a project
with billing.

The report is written to `evals/reports/`. Only `latest.md` is committed, by hand, when
a run is worth recording in the README.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jobtrack_core.config import get_settings
from jobtrack_core.ingest.extraction import ExtractedPosting
from jobtrack_core.llm import LlmBackend, build_client
from jobtrack_core.llm.client import LlmClient, LlmError
from jobtrack_core.llm.prompts import load_prompt
from jobtrack_evals.extraction import LabelledPosting, Scores, load_dataset, score_one

REPO_ROOT = Path(__file__).resolve().parents[3]
DATASET = REPO_ROOT / "evals" / "datasets" / "extraction"
REPORTS = REPO_ROOT / "evals" / "reports"


@dataclass(frozen=True, slots=True)
class Run:
    backend: str
    scores: Scores


async def run_backend(
    backend: LlmBackend, postings: list[LabelledPosting], client: LlmClient
) -> Scores:
    scores = Scores(backend=backend.value)
    prompt = load_prompt("extract_jd")

    for labelled in postings:
        posting = labelled.posting()
        rendered = prompt.render(
            posting=posting.body,
            title=posting.title or "",
            company=posting.company or "",
        )
        try:
            result = await client.generate_structured(rendered, ExtractedPosting)
        except LlmError as exc:
            # A failure is a result. Excluding it would report the score of the
            # postings that happened to work.
            scores.postings += 1
            scores.failures.append(f"{labelled.slug}: {type(exc).__name__}")
            continue

        score_one(labelled.expected, result.value, scores)

    return scores


def markdown(runs: list[Run], postings: list[LabelledPosting]) -> str:
    reviewed = sum(1 for posting in postings if posting.reviewed)
    lines = [
        "# Extraction eval",
        "",
        f"Run {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC over {len(postings)} "
        f"labelled postings, {reviewed} of them human-reviewed.",
        "",
    ]

    if reviewed < len(postings):
        lines += [
            "**Unreviewed labels are included in these numbers.** A model-drafted label "
            "scored against a model is not a measurement, so treat anything here as "
            "provisional until every label is checked.",
            "",
        ]

    lines += [
        "| backend | postings | recall | precision | F1 | must/nice | fields | hallucinated |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        s = run.scores
        if s.schema_valid == 0:
            # Every call failed. A row of zeros would read as "scored zero", which is
            # a different and much worse claim than "never answered".
            lines.append(f"| {s.backend} | {s.postings} | did not run | | | | | |")
            continue
        lines.append(
            f"| {s.backend} | {s.postings} | {s.recall:.2f} | {s.precision:.2f} | "
            f"{s.f1:.2f} | {s.kind_accuracy:.2f} | {s.field_accuracy:.2f} | "
            f"{s.hallucination_rate:.2f} |"
        )

    lines += [
        "",
        "**recall / precision** — labelled requirements found, and found requirements "
        "that were real. Matched by token overlap, because a correct extraction may trim "
        "a bullet differently than a labeller did.",
        "",
        "**must/nice** — of the requirements that were found, how many were classified "
        "correctly. The fit score weights must-haves above nice-to-haves, so this decides "
        "whether that weighting means anything.",
        "",
        "**hallucinated** — share of fields the posting leaves unstated that the backend "
        "filled in anyway. Lower is better, and this is the number that decides whether "
        "output can be shown without a warning attached.",
        "",
    ]

    if sum(1 for run in runs if run.scores.schema_valid) <= 1:
        lines += [
            "Only one backend ran. `heuristic` is a rule-based floor, not a result: it "
            "exists so that a model's score has something to be measured against. Set "
            "`LLM_BACKEND=vertex` with credentials to get the comparison.",
            "",
        ]

    for run in runs:
        if run.scores.failures:
            lines += [f"### {run.backend} failures", ""]
            lines += [f"- {failure}" for failure in run.scores.failures]
            lines.append("")

    return "\n".join(lines)


async def main_async(backends: list[LlmBackend]) -> int:
    if not DATASET.is_dir():
        print(f"No dataset at {DATASET}.", file=sys.stderr)
        return 1

    postings = load_dataset(DATASET)
    if not postings:
        print(f"No labelled postings in {DATASET}.", file=sys.stderr)
        return 1

    settings = get_settings()
    runs: list[Run] = []

    for backend in backends:
        if backend is LlmBackend.VERTEX and not settings.project_id:
            print("Skipping vertex: GOOGLE_CLOUD_PROJECT is not set.", file=sys.stderr)
            continue
        client = build_client(settings.model_copy(update={"llm_backend": backend.value}))
        try:
            scores = await run_backend(backend, postings, client)
            runs.append(Run(backend=backend.value, scores=scores))
        finally:
            await client.close()

    report = markdown(runs, postings)
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "latest.md").write_text(report, encoding="utf-8")
    (REPORTS / "latest.json").write_text(
        json.dumps([run.scores.as_dict() for run in runs], indent=2) + "\n", encoding="utf-8"
    )

    print(report)
    print(f"\nWritten to {REPORTS / 'latest.md'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        action="append",
        choices=[backend.value for backend in LlmBackend],
        help="Run only this backend. Repeatable. Defaults to heuristic plus vertex.",
    )
    arguments = parser.parse_args()
    chosen = (
        [LlmBackend(name) for name in arguments.backend]
        if arguments.backend
        else [LlmBackend.HEURISTIC, LlmBackend.VERTEX]
    )
    return asyncio.run(main_async(chosen))


if __name__ == "__main__":
    sys.exit(main())
