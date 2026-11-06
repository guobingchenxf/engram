"""Run the benchmark: `python -m engram.benchmark`."""

from __future__ import annotations

import argparse
import json

from .report import write_markdown_report
from .runner import run_benchmark


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Engram benchmark.")
    parser.add_argument("--out", default="reports/benchmark.md")
    args = parser.parse_args()

    report = run_benchmark()
    output = write_markdown_report(report, args.out)
    summary = report.aggregate(report.results)
    baseline = report.aggregate(report.baseline_results)
    ablations = {
        name: report.aggregate(results) for name, results in report.ablations.items()
    }

    print(
        json.dumps(
            {"memory": summary, "no_memory": baseline, "ablations": ablations},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
