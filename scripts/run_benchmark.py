"""Run the benchmark without installing the package.

    python scripts/run_benchmark.py --out reports/benchmark.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engram.benchmark.report import write_markdown_report
from engram.benchmark.runner import run_benchmark
from engram.viewer import render_benchmark_html


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Engram benchmark.")
    parser.add_argument("--out", default="reports/benchmark.md")
    parser.add_argument("--html", default="reports/benchmark.html")
    args = parser.parse_args()

    report = run_benchmark()
    output = write_markdown_report(report, args.out)
    html_output = Path(args.html)
    html_output.parent.mkdir(parents=True, exist_ok=True)
    html_output.write_text(render_benchmark_html(report), encoding="utf-8")
    print(report.aggregate(report.results))
    print(report.aggregate(report.baseline_results))
    print(f"report: {output}")
    print(f"html: {html_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
