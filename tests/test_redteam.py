from __future__ import annotations

from engram.redteam import REDTEAM_CASES, run_redteam, summarize
from engram.service import MemoryService


def test_redteam_suite_passes(service: MemoryService) -> None:
    results = run_redteam(service)
    failures = [result for result in results if not result.passed]
    assert not failures, failures
    assert len(results) == len(REDTEAM_CASES)


def test_redteam_summary(service: MemoryService) -> None:
    summary = summarize(run_redteam(service))
    assert summary["pass_rate"] == 1.0
    assert summary["total"] == len(REDTEAM_CASES)
    assert "prompt_injection" in summary["by_category"]
