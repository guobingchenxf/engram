from __future__ import annotations

from fastapi.testclient import TestClient

from engram.api import create_app
from engram.benchmark.metrics import (
    BenchmarkReport,
    CheckResult,
    ScenarioResult,
)
from engram.config import MemoryConfig
from engram.viewer import render_benchmark_html, render_trace_html


def test_render_empty_trace() -> None:
    page = render_trace_html([])
    assert "Engram trace" in page
    assert "No trace events yet" in page


def test_render_trace_events() -> None:
    page = render_trace_html(
        [
            {
                "ts": "2026-11-05T00:00:00+00:00",
                "event": "write",
                "user_id": "u1",
                "memory_id": "m1",
                "type": "semantic",
            }
        ]
    )
    assert "write" in page
    assert "u1" in page
    assert "m1" in page


def test_api_trace_view(config: MemoryConfig) -> None:
    client = TestClient(create_app(config))
    response = client.get("/v1/trace/view")
    assert response.status_code == 200
    assert "Engram trace" in response.text


def test_benchmark_html_renders() -> None:
    check = CheckResult(
        scenario_id="s1",
        category="category",
        query="q",
        passed=True,
        expected_hit=True,
        forbidden_hit=False,
        hits=1,
        tokens=10,
        min_hits_ok=True,
        max_hits_ok=True,
    )
    scenario = ScenarioResult(
        scenario_id="s1",
        category="category",
        passed=True,
        checks=[check],
    )
    report = BenchmarkReport(
        results=[scenario],
        baseline_results=[scenario],
        ablations={"vector_only": [scenario]},
    )
    page = render_benchmark_html(report)
    assert "Engram benchmark" in page
    assert "s1" in page
    assert "vector_only" in page
