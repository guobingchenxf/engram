from __future__ import annotations

from engram.benchmark.runner import run_benchmark
from engram.benchmark.scenarios import SCENARIOS


def test_scenario_set_shape() -> None:
    assert len(SCENARIOS) == 23
    assert {scenario.category for scenario in SCENARIOS} == {
        "preference_change",
        "contradiction",
        "long_gap",
        "task_continuity",
        "privacy",
        "abstention",
        "hard_retrieval",
    }
    assert all(scenario.checks for scenario in SCENARIOS)


def test_benchmark_report_shape() -> None:
    report = run_benchmark()
    assert len(report.results) == 23
    data = report.to_dict()
    assert data["memory"]["aggregate"]["checks"] > 0
    assert data["no_memory"]["aggregate"]["checks"] > 0
    assert "check_pass_rate" in data["memory"]["aggregate"]
    assert data["memory"]["aggregate"]["scenarios_passed"] == 23
    assert data["memory"]["aggregate"]["checks_passed"] == 23
    assert (
        data["ablations"]["no_conflicts"]["aggregate"]["scenarios_passed"] < 23
    )
    assert {"vector_only", "no_conflicts", "no_rerank", "no_sensitivity_filter"} <= set(
        data["ablations"]
    )
