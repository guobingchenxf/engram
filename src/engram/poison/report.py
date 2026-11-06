"""Markdown leaderboard for MemPoison."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .cases import build_cases
from .runner import PoisonReport


def render_markdown(report: PoisonReport) -> str:
    cases = build_cases()
    channels = Counter(case.channel for case in cases)
    strategies = Counter(case.strategy for case in cases)
    goals = Counter(case.goal for case in cases)

    lines = [
        "# MemPoison Leaderboard",
        "",
        "> Memory poisoning benchmark for LLM agents. Lower ASR is better; higher",
        "> UR is better. FPR is the fraction of benign memories rejected.",
        "",
        "## Dataset",
        "",
        f"- cases: **{len(cases)}** (108 base + 12 adaptive)",
        f"- channels: {dict(sorted(channels.items()))}",
        f"- strategies: {dict(sorted(strategies.items()))}",
        f"- goals: {dict(sorted(goals.items()))}",
        "",
        "## Leaderboard",
        "",
        "| Defense | ASR ↓ | Adaptive ASR ↓ | UR ↑ | FPR ↓ | Detection rate | Avg latency |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for summary in report.summaries:
        lines.append(
            f"| {summary.defense} | {summary.attack_success_rate:.1%} | "
            f"{summary.adaptive_asr:.1%} | {summary.utility_retention:.1%} | "
            f"{summary.false_positive_rate:.1%} | "
            f"{summary.detection_rate:.1%} | {summary.avg_detection_latency:.2f} |"
        )

    lines.extend(
        [
            "",
            "## What the columns mean",
            "",
            "- **ASR**: attack success rate, the fraction of cases where the victim",
            "  follows the poisoned memory instead of the benign answer.",
            "- **UR**: utility retention, the fraction of cases where the benign",
            "  answer survives.",
            "- **FPR**: false positive rate on benign memories.",
            "- **Detection rate**: fraction of cases where the defense blocks or",
            "  isolates the poison.",
            "- **Avg detection latency**: average poisoned-memory index at which the",
            "  defense acts (1 = first write; lower is better).",
            "",
            "## Conditions",
            "",
            "- `none`: safety disabled; every write is accepted.",
            "- `source_trust`: the existing SafetyGuard trust policy.",
            "- `classifier`: source trust + write-time poison classifier.",
            "- `isolation`: writes are accepted, but untrusted sources are removed",
            "  from the context before it reaches the victim agent.",
            "",
            "## Honest limits",
            "",
            "- The victim is a deterministic simulator, not an LLM. It models a",
            "  memory-following failure, not full model compliance.",
            "- The adaptive split forges a trusted provenance parent; provenance",
            "  alone does not stop it. Integrity checks/signatures are required.",
            "- Integrity checks and consensus voting are not implemented yet.",
            "- The dataset is synthetic and should be paired with an external",
            "  human-annotated benchmark before publication claims.",
            "- No comparison with Mem0/Zep/Letta is claimed here; the benchmark is",
            "  the artifact, not a leaderboard of vendors.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_markdown_report(report: PoisonReport, path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(report), encoding="utf-8")
    return output
