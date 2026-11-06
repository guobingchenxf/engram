# Stage 2 Report: First Real Benchmark

Date: 2026-11-05

> Note: sections 2–4 report the original 20-scenario base set. The hardened
> 23-scenario results are in section 5.

## 1. What was run

```bash
python -m engram.benchmark --out reports/benchmark.md
```

Configuration: deterministic/offline (hash embeddings + heuristic extraction),
fresh temporary SQLite database per scenario.

Conditions:

- **memory**: full lifecycle (conflict/supersede, sensitivity filter, hybrid
  retrieval, token budget, MMR);
- **no_memory**: every user turn placed into the context (full-history baseline);
- **vector_only**: retrieval weights `relevance=1.0`, all other signals 0, no MMR;
- **no_conflicts**: `enable_conflicts=false`;
- **no_rerank**: `mmr_lambda=1.0`;
- **no_sensitivity_filter**: every check queries with `include_sensitive=true`.

## 2. Before / after

| Run | Memory scenarios | Full-history | Notes |
|---|---:|---:|---|
| First run | 19/20 (95%) | 5/20 (25%) | `salary_conflict` failed |
| After fix | **20/20 (100%)** | 5/20 (25%) | `salary_conflict` passes |

### The failure

`salary_conflict` queried "我的期望薪资" and got **0 hits**. Both salary
memories existed and the conflict had superseded the old one, but salary is
classified as `SENSITIVE` by the safety guard (the keyword list includes
"薪资"/"工资"). Retrieval excludes sensitive memories by default, so the new
memory was filtered out.

### The fix

- Added `include_sensitive` to `benchmark.scenarios.Check` (default `false`).
- `salary_conflict` explicitly sets `include_sensitive=true`, because this test
  is about conflict resolution, not about the privacy filter.
- The runner passes the flag through to `MemoryQuery`.

This is the correct behavior: a sensitive memory should not be injected unless
the caller explicitly asks for it. The benchmark now tests both:
privacy scenarios use the default filter; the salary conflict test opts in.

## 3. Ablations

| Condition | Scenarios passed | Checks passed | Forbidden hit rate |
|---|---:|---:|---:|
| memory | 20/20 | 20/20 | 0.00% |
| no_memory | 5/20 | 5/20 | 100.00% |
| vector_only | 20/20 | 20/20 | 0.00% |
| no_conflicts | 10/20 | 10/20 | 100.00% |
| no_rerank | 20/20 | 20/20 | 0.00% |
| no_sensitivity_filter | 18/20 | 18/20 | 100.00% |

### What this says

1. **Conflict lifecycle is load-bearing.** Disabling it drops from 20/20 to
   10/20: stale preferences and contradictions re-enter the context.
2. **Sensitivity filtering is load-bearing.** Disabling it drops privacy
   scenarios and produces forbidden content.
3. **Retrieval ranking is not stressed.** `vector_only` and `no_rerank` are
   identical to the full system because every query has one clearly relevant
   memory. The next benchmark iteration needs distractor-heavy scenarios:
   several projects with different deadlines, several model/metric pairs,
   several similar preferences.
4. **Forgetting is not ablated.** The current scenarios do not advance time or
   exercise TTL. A time-controlled `no_forget` condition is future work.

## 4. Honest interpretation

- 20/20 is a **regression-suite result**, not a claim that memory is solved.
- The baseline is deliberately naive (full history); it is a useful contrast for
  stale facts and privacy, not a state-of-the-art competitor.
- Substring checks are deterministic but shallow; an LLM judge and human
  annotation are needed before a publishable claim.
- Hash embeddings are semantically weak; retrieval quality numbers do not
  transfer to a real embedding model.
- The `salary_conflict` fix is a product-policy correction, not a model
  improvement.

## 5. Stage 2.5: hard retrieval iteration (same day)

Added three distractor-heavy scenarios:

- `hard_project_deadlines`: three projects with different deadlines;
- `hard_metric_table`: three models with different metrics;
- `hard_preference_domains`: language vs food vs editor preference.

First hard run: **20/23**. All three hard scenarios failed because the conflict
detector treated different subjects as the same fact:

- Project Alpha vs Project Beta deadlines were treated as a numeric conflict;
- MMoE parameters vs DIN AUC vs DeepFM latency were treated as a conflict;
- language preference vs food/editor preference were treated as a value
  conflict.

Fixes:

1. Entity/subject check: if both memories have entities and share none, do not
   treat them as comparable facts.
2. Attribute check: numeric/value conflicts require the same explicit attribute
   (language, goal, salary, weekly availability, project, AUC, parameter count,
   latency, model).
3. Hard checks use `top_k=1` so they test precision rather than dumping every
   distractor into the context.

After fix: **23/23**; full-history 5/23.

Ablations after hardening:

| Condition | Scenarios passed |
|---|---:|
| memory | 23/23 |
| no_conflicts | 13/23 |
| no_sensitivity_filter | 21/23 |
| vector_only | 23/23 |
| no_rerank | 23/23 |

`vector_only` and `no_rerank` still do not diverge. Reason: hash embeddings are
lexical, and even distractor-heavy hard scenarios contain exact query tokens
that both retrieval paths match. Differentiating these ablations requires a real
embedding model and redundancy-heavy scenarios, not more substring distractors.

## 6. Next

1. Add a real embedding provider smoke test; revisit `vector_only`/`no_rerank`.
2. Add a time-controlled TTL scenario and a `no_forget` ablation.
3. Add an LLM judge with position/length-bias calibration.
4. Move to Stage 3: CI, Docker, packaging, auth, provider retries.
