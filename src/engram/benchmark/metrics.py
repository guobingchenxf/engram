"""Benchmark result structures and aggregate metrics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CheckResult:
    scenario_id: str
    category: str
    query: str
    passed: bool
    expected_hit: bool
    forbidden_hit: bool
    hits: int
    tokens: int
    min_hits_ok: bool
    max_hits_ok: bool
    expected_count: int = 0
    missing: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    context_preview: str = ""


@dataclass
class ScenarioResult:
    scenario_id: str
    category: str
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    memory_count: int = 0
    conflict_count: int = 0
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkReport:
    results: list[ScenarioResult] = field(default_factory=list)
    baseline_results: list[ScenarioResult] = field(default_factory=list)
    ablations: dict[str, list[ScenarioResult]] = field(default_factory=dict)

    def aggregate(self, results: list[ScenarioResult] | None = None) -> dict[str, Any]:
        selected = results if results is not None else self.results
        checks = [check for scenario in selected for check in scenario.checks]
        total = len(checks)
        passed = sum(1 for check in checks if check.passed)
        expected_checks = [check for check in checks if check.expected_count > 0]
        forbidden_checks = [check for check in checks if check.forbidden]

        by_category: dict[str, dict[str, int]] = {}
        for scenario in selected:
            bucket = by_category.setdefault(
                scenario.category, {"total": 0, "passed": 0}
            )
            bucket["total"] += 1
            bucket["passed"] += int(scenario.passed)

        return {
            "scenarios": len(selected),
            "scenarios_passed": sum(1 for scenario in selected if scenario.passed),
            "checks": total,
            "checks_passed": passed,
            "check_pass_rate": (passed / total) if total else 0.0,
            "expected_hit_rate": (
                sum(1 for check in expected_checks if check.expected_hit)
                / len(expected_checks)
                if expected_checks
                else 0.0
            ),
            "forbidden_hit_rate": (
                sum(1 for check in forbidden_checks if check.forbidden_hit)
                / len(forbidden_checks)
                if forbidden_checks
                else 0.0
            ),
            "avg_hits": (sum(check.hits for check in checks) / total) if total else 0.0,
            "avg_tokens": (sum(check.tokens for check in checks) / total) if total else 0.0,
            "by_category": by_category,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_count": len(self.results),
            "memory": {
                "aggregate": self.aggregate(self.results),
                "scenarios": [asdict(result) for result in self.results],
            },
            "no_memory": {
                "aggregate": self.aggregate(self.baseline_results),
                "scenarios": [asdict(result) for result in self.baseline_results],
            },
            "ablations": {
                name: {
                    "aggregate": self.aggregate(results),
                    "scenarios": [asdict(result) for result in results],
                }
                for name, results in self.ablations.items()
            },
        }
