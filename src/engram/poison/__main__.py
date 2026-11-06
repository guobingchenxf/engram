"""Run MemPoison: `python -m engram.poison`."""

from __future__ import annotations

import argparse
import json

from .annotation import write_annotation_template, write_heldout
from .cases import build_cases, write_jsonl
from .report import write_markdown_report
from .runner import run_poison_benchmark


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the MemPoison benchmark.")
    parser.add_argument("--cases", default="bench/poison/cases.jsonl")
    parser.add_argument("--out", default="reports/poison.md")
    parser.add_argument("--heldout", default="bench/poison/heldout.jsonl")
    parser.add_argument(
        "--annotation-template",
        default="bench/poison/annotation_template.csv",
    )
    args = parser.parse_args()

    cases = build_cases()
    cases_path = write_jsonl(args.cases, cases)
    heldout_path = write_heldout(args.heldout, cases)
    annotation_path = write_annotation_template(args.annotation_template)
    report = run_poison_benchmark(cases)
    output = write_markdown_report(report, args.out)
    print(
        json.dumps(
            [summary.__dict__ for summary in report.summaries],
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"cases: {cases_path}")
    print(f"heldout: {heldout_path}")
    print(f"annotation template: {annotation_path}")
    print(f"leaderboard: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
