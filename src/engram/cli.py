"""Command-line interface for Engram."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import Any

from .config import MemoryConfig
from .models import (
    ExtractRequest,
    MemoryQuery,
    MemoryType,
    MemoryWriteRequest,
    SourceType,
)
from .service import MemoryService


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _service(args: argparse.Namespace) -> MemoryService:
    config = MemoryConfig.from_env()
    overrides: dict[str, Any] = {}
    if getattr(args, "db", None):
        overrides["db_path"] = args.db
    if getattr(args, "trace", None):
        overrides["trace_path"] = args.trace
    if overrides:
        config = config.with_overrides(**overrides)
    return MemoryService(config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engram", description=__doc__)
    parser.add_argument("--db", help="override MEMORY_DB_PATH")
    parser.add_argument("--trace", help="override MEMORY_TRACE_PATH")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="initialize the database and print stats")

    write = subparsers.add_parser("write", help="write one explicit memory")
    write.add_argument("--user-id", required=True)
    write.add_argument("--content", required=True)
    write.add_argument("--type", choices=[item.value for item in MemoryType], default="semantic")
    write.add_argument(
        "--source-type",
        choices=[item.value for item in SourceType],
        default="user",
    )
    write.add_argument("--source")
    write.add_argument("--confidence", type=float)
    write.add_argument("--importance", type=float)

    search = subparsers.add_parser("search", help="search memories")
    search.add_argument("--user-id", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--top-k", type=int, default=8)
    search.add_argument("--budget", type=int, default=800)

    explain = subparsers.add_parser(
        "explain", help="counterfactual influence (leave-one-out or shapley)"
    )
    explain.add_argument("--user-id", required=True)
    explain.add_argument("--query", required=True)
    explain.add_argument("--top-k", type=int, default=5)
    explain.add_argument("--budget", type=int, default=500)
    explain.add_argument(
        "--method",
        choices=["leave_one_out", "shapley"],
        default="leave_one_out",
    )
    explain.add_argument("--samples", type=int, default=12)

    extract = subparsers.add_parser("extract", help="extract and write memories from text")
    extract.add_argument("--user-id", required=True)
    extract.add_argument("--text", required=True)
    extract.add_argument(
        "--source-type",
        choices=[item.value for item in SourceType],
        default="user",
    )
    extract.add_argument("--no-write", action="store_true")

    stats = subparsers.add_parser("stats", help="show memory statistics")
    stats.add_argument("--user-id")

    maintenance = subparsers.add_parser("maintenance", help="expire and decay memories")
    maintenance.add_argument("--prune", action="store_true")

    conflicts = subparsers.add_parser("conflicts", help="list conflicts")
    conflicts.add_argument("--user-id")
    conflicts.add_argument("--status")

    resolve = subparsers.add_parser("resolve", help="resolve a conflict")
    resolve.add_argument("conflict_id")
    resolve.add_argument(
        "--strategy",
        choices=["keep_new", "keep_old", "keep_both"],
        default="keep_new",
    )

    subparsers.add_parser("demo", help="run the scripted multi-session demo")
    subparsers.add_parser("redteam", help="run the safety red-team suite")

    trace_view = subparsers.add_parser("trace-view", help="render the JSONL trace as HTML")
    trace_view.add_argument("--out", default="reports/trace.html")
    trace_view.add_argument("--limit", type=int, default=200)

    bench = subparsers.add_parser("bench", help="run the scenario benchmark")
    bench.add_argument("--out", default="reports/benchmark.md")
    bench.add_argument("--html", default="reports/benchmark.html")

    poison = subparsers.add_parser("poison", help="run the MemPoison benchmark")
    poison.add_argument("--cases", default="bench/poison/cases.jsonl")
    poison.add_argument("--out", default="reports/poison.md")
    poison.add_argument("--heldout", default="bench/poison/heldout.jsonl")
    poison.add_argument(
        "--annotation-template",
        default="bench/poison/annotation_template.csv",
    )
    poison.add_argument(
        "--victim",
        choices=["deterministic", "llm"],
        default="deterministic",
    )

    submit = subparsers.add_parser(
        "poison-submit", help="validate a MemPoison submission JSON"
    )
    submit.add_argument("path")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "demo":
        from .demo_agent import run_demo

        run_demo()
        return 0

    if args.command == "bench":
        from pathlib import Path

        from .benchmark.report import write_markdown_report
        from .benchmark.runner import run_benchmark
        from .viewer import render_benchmark_html

        report = run_benchmark()
        write_markdown_report(report, args.out)
        html_path = Path(args.html)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(render_benchmark_html(report), encoding="utf-8")
        _print({"report": str(args.out), "html": str(html_path)})
        return 0

    if args.command == "poison":
        from .errors import ConfigurationError
        from .llm import get_llm
        from .poison.annotation import write_annotation_template, write_heldout
        from .poison.cases import build_cases, write_jsonl
        from .poison.report import write_markdown_report as write_poison_report
        from .poison.runner import run_poison_benchmark
        from .poison.victim import LLMVictim

        victim = None
        if args.victim == "llm":
            try:
                llm = get_llm(
                    MemoryConfig.from_env().with_overrides(llm_provider="openai")
                )
            except ConfigurationError as exc:
                _print({"error": str(exc)})
                return 1
            if llm is None:
                _print({"error": "LLM provider is not configured"})
                return 1
            victim = LLMVictim(llm)

        poison_cases = build_cases()
        cases_path = write_jsonl(args.cases, poison_cases)
        heldout_path = write_heldout(args.heldout, poison_cases)
        annotation_path = write_annotation_template(args.annotation_template)
        poison_report = run_poison_benchmark(poison_cases, victim=victim)
        poison_output = write_poison_report(poison_report, args.out)
        _print(
            {
                "cases": str(cases_path),
                "heldout": str(heldout_path),
                "annotation_template": str(annotation_path),
                "leaderboard": str(poison_output),
                "victim": args.victim,
                "defenses": [
                    summary.__dict__ for summary in poison_report.summaries
                ],
            }
        )
        return 0

    if args.command == "poison-submit":
        from .poison.submission import load_submission

        try:
            submission = load_submission(args.path)
        except (OSError, ValueError) as exc:
            _print({"error": str(exc)})
            return 1
        _print(submission.model_dump())
        return 0

    service = _service(args)

    if args.command == "init":
        _print(service.stats())
    elif args.command == "explain":
        from .debug import CounterfactualDebugger

        explanation = CounterfactualDebugger(service, args.user_id).explain(
            args.query,
            top_k=args.top_k,
            token_budget=args.budget,
            method=args.method,
            samples=args.samples,
        )
        _print(explanation.to_dict())
    elif args.command == "trace-view":
        from pathlib import Path

        from .viewer import render_trace_html

        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            render_trace_html(service.trace.read(limit=args.limit)),
            encoding="utf-8",
        )
        _print({"path": str(output), "limit": args.limit})
    elif args.command == "redteam":
        from dataclasses import asdict

        from .redteam import run_redteam, summarize

        results = run_redteam(service)
        _print(
            {
                "summary": summarize(results),
                "results": [asdict(result) for result in results],
            }
        )
    elif args.command == "write":
        record = service.write(
            MemoryWriteRequest(
                user_id=args.user_id,
                content=args.content,
                type=args.type,
                source_type=args.source_type,
                source=args.source,
                confidence=args.confidence,
                importance=args.importance,
            )
        )
        _print(record.model_dump(mode="json"))
    elif args.command == "search":
        result = service.search(
            MemoryQuery(
                user_id=args.user_id,
                query=args.query,
                top_k=args.top_k,
                token_budget=args.budget,
            )
        )
        _print(result.model_dump(mode="json"))
    elif args.command == "extract":
        records = service.extract_and_write(
            ExtractRequest(
                user_id=args.user_id,
                text=args.text,
                source_type=args.source_type,
                auto_write=not args.no_write,
            )
        )
        _print([record.model_dump(mode="json") for record in records])
    elif args.command == "stats":
        _print(service.stats(args.user_id))
    elif args.command == "maintenance":
        _print(service.maintenance(prune=args.prune))
    elif args.command == "conflicts":
        _print(
            [
                conflict.model_dump(mode="json")
                for conflict in service.conflicts(
                    user_id=args.user_id, status=args.status
                )
            ]
        )
    elif args.command == "resolve":
        conflict = service.resolve_conflict(args.conflict_id, args.strategy)
        _print(conflict.model_dump(mode="json") if conflict else None)
    else:  # pragma: no cover - argparse prevents this
        parser.error(f"unknown command: {args.command}")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
