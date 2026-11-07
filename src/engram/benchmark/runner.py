"""Benchmark runner: memory vs full-history baseline."""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..config import MemoryConfig, RetrievalWeights
from ..embeddings import estimate_tokens
from ..models import ExtractRequest, MemoryQuery, SourceType
from ..service import MemoryService
from .metrics import BenchmarkReport, CheckResult, ScenarioResult
from .scenarios import SCENARIOS, Check, Scenario

ABLATIONS: dict[str, dict[str, object]] = {
    "vector_only": {
        "retrieval_weights": RetrievalWeights(
            relevance=1.0,
            recency=0.0,
            importance=0.0,
            confidence=0.0,
        ),
        "mmr_lambda": 1.0,
    },
    "no_conflicts": {"enable_conflicts": False},
    "no_rerank": {"mmr_lambda": 1.0},
    "no_sensitivity_filter": {"include_sensitive": True},
}


def run_benchmark(
    config: MemoryConfig | None = None,
    *,
    deterministic: bool = True,
) -> BenchmarkReport:
    """Run all scenarios with memory and with a full-history baseline.

    The default is deterministic/offline (hash embeddings + heuristic
    extraction) so results are reproducible without API keys.
    """
    base_config = config or MemoryConfig.from_env()
    if deterministic:
        base_config = base_config.with_overrides(
            embedding_provider="hash",
            llm_provider="heuristic",
        )

    memory_results: list[ScenarioResult] = []
    baseline_results: list[ScenarioResult] = []
    ablation_results: dict[str, list[ScenarioResult]] = {
        name: [] for name in ABLATIONS
    }

    for scenario in SCENARIOS:
        memory_results.append(_run_condition(scenario, base_config))
        for name, overrides in ABLATIONS.items():
            include_sensitive = bool(overrides.get("include_sensitive", False))
            config_overrides = {
                key: value
                for key, value in overrides.items()
                if key != "include_sensitive"
            }
            ablation_results[name].append(
                _run_condition(
                    scenario,
                    base_config,
                    overrides=config_overrides,
                    include_sensitive=include_sensitive,
                )
            )
        baseline_results.append(_evaluate_full_history(scenario))

    return BenchmarkReport(
        results=memory_results,
        baseline_results=baseline_results,
        ablations=ablation_results,
    )


def _run_condition(
    scenario: Scenario,
    base_config: MemoryConfig,
    *,
    overrides: dict[str, object] | None = None,
    include_sensitive: bool = False,
) -> ScenarioResult:
    with tempfile.TemporaryDirectory(prefix=f"engram-{scenario.id}-") as tmp:
        config_for_scenario = base_config.with_overrides(
            db_path=str(Path(tmp) / "memory.db"),
            trace_path=str(Path(tmp) / "trace.jsonl"),
            **(overrides or {}),
        )
        service = MemoryService(config_for_scenario)
        try:
            _ingest_scenario(service, scenario)
            return _evaluate_with_memory(
                service, scenario, include_sensitive=include_sensitive
            )
        finally:
            service.close()


def _ingest_scenario(service: MemoryService, scenario: Scenario) -> None:
    for session in scenario.sessions:
        for turn in session.turns:
            if turn.role != "user":
                continue
            service.extract_and_write(
                ExtractRequest(
                    user_id=scenario.user_id,
                    text=turn.content,
                    source_type=SourceType.USER,
                    source="benchmark",
                )
            )


def _evaluate_with_memory(
    service: MemoryService,
    scenario: Scenario,
    *,
    include_sensitive: bool = False,
) -> ScenarioResult:
    checks: list[CheckResult] = []
    for check in scenario.checks:
        result = service.search(
            MemoryQuery(
                user_id=scenario.user_id,
                query=check.query,
                top_k=check.top_k,
                token_budget=check.token_budget,
                include_sensitive=check.include_sensitive or include_sensitive,
            )
        )
        checks.append(
            _evaluate_check(
                scenario,
                check,
                context=result.context,
                hits=len(result.hits),
                tokens=result.total_tokens,
            )
        )

    stats = service.stats(scenario.user_id)
    return ScenarioResult(
        scenario_id=scenario.id,
        category=scenario.category,
        passed=all(check.passed for check in checks),
        checks=checks,
        memory_count=int(stats.get("total", 0)),
        conflict_count=int(stats.get("conflicts_open", 0)),
        stats=stats,
    )


def _evaluate_full_history(scenario: Scenario) -> ScenarioResult:
    history = "\n".join(
        turn.content
        for session in scenario.sessions
        for turn in session.turns
        if turn.role == "user"
    )
    hits = 1 if history.strip() else 0
    tokens = estimate_tokens(history)
    checks = [
        _evaluate_check(
            scenario,
            check,
            context=history,
            hits=hits,
            tokens=tokens,
        )
        for check in scenario.checks
    ]
    return ScenarioResult(
        scenario_id=scenario.id,
        category=scenario.category,
        passed=all(check.passed for check in checks),
        checks=checks,
        memory_count=0,
        conflict_count=0,
        stats={"total": 0, "conflicts_open": 0, "source": "full_history"},
    )


def _evaluate_check(
    scenario: Scenario,
    check: Check,
    *,
    context: str,
    hits: int,
    tokens: int,
) -> CheckResult:
    missing = [substring for substring in check.expected_substrings if substring not in context]
    forbidden = [substring for substring in check.forbidden_substrings if substring in context]

    min_hits_ok = hits >= check.min_hits
    max_hits_ok = check.max_hits is None or hits <= check.max_hits
    expected_hit = not missing if check.expected_substrings else True
    forbidden_hit = bool(forbidden)
    passed = min_hits_ok and max_hits_ok and expected_hit and not forbidden_hit

    return CheckResult(
        scenario_id=scenario.id,
        category=scenario.category,
        query=check.query,
        passed=passed,
        expected_hit=expected_hit,
        forbidden_hit=forbidden_hit,
        hits=hits,
        tokens=tokens,
        min_hits_ok=min_hits_ok,
        max_hits_ok=max_hits_ok,
        expected_count=len(check.expected_substrings),
        missing=missing,
        forbidden=forbidden,
        context_preview=context[:500],
    )
