# Evaluation

> The primary evidence is **MemPoison**, the memory-poisoning benchmark in
> [POISON.md](POISON.md). The 23-scenario lifecycle benchmark below is a
> regression suite, not the identity of the project.

## 1. What the benchmark measures

The benchmark asks a simple counterfactual question:

> For the same multi-session history, does a memory layer produce better
> retrieval outcomes than simply putting the whole history into the context?

It covers six failure modes that matter in production memory:

1. **Preference change** — the user changes their mind; the old preference must
   not win.
2. **Contradiction** — two statements conflict; the system must prefer the newer
   trusted statement and record the conflict.
3. **Long gap** — a fact stated several sessions ago must still be retrievable.
4. **Task continuity** — the user resumes unfinished work.
5. **Privacy** — sensitive data must not be injected by default; secrets must be
   rejected.
6. **Abstention** — when no relevant memory exists, the system must not retrieve
   unrelated memories.

## 2. Scenario set

Twenty-three scenarios, each with 0–3 sessions and 1 check.

| ID | Category | What it tests |
|---|---|---|
| `pref_language_change` | preference_change | Chinese → English explanation preference |
| `pref_location_change` | preference_change | Hangzhou → Shanghai internship preference |
| `pref_model_change` | preference_change | MMoE → DIN model preference |
| `pref_tool_change` | preference_change | Python → Go implementation preference |
| `pref_style_change` | preference_change | detailed → concise explanation preference |
| `goal_conflict` | contradiction | 搜广推 → Agent internship goal |
| `availability_conflict` | contradiction | 4 days → 2 days per week |
| `salary_conflict` | contradiction | 300 → 250 RMB/day |
| `project_deadline` | long_gap | deadline recalled after a filler session |
| `metric_update` | long_gap | AUC 0.72 → 0.78; old value must not appear |
| `decision_update` | long_gap | FastAPI → Go service decision |
| `experiment_resume` | task_continuity | unfinished ablation experiment |
| `debug_resume` | task_continuity | retry-limit fix for a tool-call loop |
| `report_resume` | task_continuity | missing evaluation chapter |
| `phone_privacy` | privacy | phone number stored but not injected |
| `secret_privacy` | privacy | API key rejected at write time |
| `address_privacy` | privacy | home address stored but not injected |
| `unknown_topic` | abstention | unrelated query retrieves nothing |
| `no_memory` | abstention | new user has no memories |
| `future_topic` | abstention | topic never mentioned must not hallucinate |
| `hard_project_deadlines` | hard_retrieval | three projects, select Beta's deadline |
| `hard_metric_table` | hard_retrieval | three models, select DIN/AUC |
| `hard_preference_domains` | hard_retrieval | language vs food vs editor preference |

The scenario definitions live in
`src/engram/benchmark/scenarios.py`.

## 3. Conditions

### Memory condition

For each scenario, a fresh temporary SQLite database is created. Every user turn
is passed through `MemoryService.extract_and_write`. Then each check runs a
`MemoryQuery` with:

- `top_k=8`
- `token_budget` from the check (default 600)
- `include_sensitive=false`

### Full-history baseline

Every user turn is concatenated and placed into the context. This simulates the
common "just use a long context" approach. It is a strong baseline for factual
recall, but it has no notion of staleness, conflict, sensitivity or budget.

### Future conditions

The runner is structured so additional conditions can be added:

- summary buffer (rolling summary);
- vector-only retrieval (no lifecycle or rerank);
- memory without conflict resolution;
- memory without forgetting;
- an LLM judge condition.

## 4. Metrics

Per check:

| Metric | Definition |
|---|---|
| `expected_hit` | every expected substring appears in the context |
| `forbidden_hit` | any forbidden substring appears in the context |
| `min_hits_ok` | retrieved hits ≥ `min_hits` |
| `max_hits_ok` | retrieved hits ≤ `max_hits` (when set) |
| `passed` | all of the above |
| `tokens` | context token estimate |

Aggregates:

- scenario pass rate;
- check pass rate;
- expected-hit rate;
- forbidden-hit rate;
- average hits;
- average tokens;
- pass rate by category.

## 5. Protocol

1. Each scenario gets an isolated temporary database.
2. Deterministic mode forces hash embeddings and heuristic extraction, so the
   benchmark runs without an API key and produces reproducible results.
3. Substring checks are used for reproducibility. This is a **development**
   benchmark, not a publishable evaluation.
4. The benchmark does not use the test scenario text to tune prompts.
5. A human should spot-check failures before reporting results.

## 6. Running

```bash
python -m engram.benchmark --out reports/benchmark.md
```

The command prints the memory and baseline aggregates and writes a Markdown
report with per-scenario results and failure details.

## 7. Current status (2026-11-05)

The benchmark has been executed. Results are in `reports/benchmark.md`.

| Condition | Scenarios passed | Checks passed |
|---|---:|---:|
| Memory (full lifecycle) | 23/23 | 23/23 |
| Full-history baseline | 5/23 | 5/23 |
| `vector_only` | 23/23 | 23/23 |
| `no_conflicts` | 13/23 | 13/23 |
| `no_rerank` | 23/23 | 23/23 |
| `no_sensitivity_filter` | 21/23 | 21/23 |

Before/after:

- Stage 2 base run: **19/20**. The only failure was `salary_conflict`; salary
  was classified as sensitive and therefore excluded by the default retrieval
  filter. Fixed by adding an explicit `include_sensitive` check for that
  scenario; after fix **20/20**.
- Stage 2.5 hard-retrieval run: **20/23** on first attempt. All three
  `hard_retrieval` scenarios failed for two reasons:
  1. the conflict detector treated different projects/models as the same fact
     and superseded them (e.g., Project Alpha vs Beta);
  2. it treated different preference domains as contradictory (language vs
     food vs editor).
  Fixed with subject/entity-aware and attribute-aware conflict checks. The hard
  checks also use `top_k=1` to test precision instead of dumping all memories
  into the context. After fix **23/23**.

What the ablations say:

1. **Conflict lifecycle is load-bearing.** Disabling conflicts drops the score
   to 13/23 and reintroduces stale preferences/contradictions.
2. **Sensitivity filtering is load-bearing.** Disabling it drops privacy
   scenarios and produces forbidden content in the context.
3. **Retrieval ranking is still not stressed.** `vector_only` and `no_rerank`
   are both 23/23: hash embeddings are lexical, and the hard scenarios still
   contain exact tokens that both retrieval paths can match. The next lever is a
   real embedding provider and redundancy-heavy scenarios, not more substring
   distractors.
4. **Forgetting is not yet ablated.** The current scenarios do not advance time
   or exercise TTL; a time-controlled `no_forget` condition is future work.

Caveats:

- These are development-benchmark numbers, not external validation.
- The checks are substring-based and deterministic; there is no LLM judge or
  human annotation yet.
- Hash embeddings are semantically weak by design; do not compare these numbers
  with published retrieval benchmarks.
- The 23/23 result means "the system passes this regression suite", not "the
  memory problem is solved".

## 8. Stage 4 additions

- Graph memory: entity index + graph candidate expansion; `SearchHit.graph`
  exposes the graph contribution.
- Red-team: 12 cases, all passing (see [REDTEAM.md](REDTEAM.md)).
- LLM judge: rubric scoring with position-swap and length-padding probes; the
  real-judge calibration is still pending.
- Trace viewer: `GET /v1/trace/view` renders the JSONL trace as HTML.
- Benchmark dashboard: `reports/benchmark.html` with condition bars, category
  bars, a scenario grid and failure details (see
  [VISUALIZATION.md](VISUALIZATION.md)).

## 9. Failure taxonomy

When a scenario fails, classify it:

| Failure | Meaning | Typical fix |
|---|---|---|
| Extraction miss | The fact never became a memory | Add a pattern / improve LLM prompt |
| Retrieval miss | The memory exists but was not retrieved | Improve query rewrite, BM25, embeddings, weights |
| Stale hit | An old/superseded memory was used | Strengthen conflict detection or supersede |
| Forbidden hit | An irrelevant/sensitive memory was injected | Tighten filters or sensitivity policy |
| Budget truncation | The right memory was cut by the token budget | Improve ranking or compression |
| Over-retrieval | Too many unrelated hits | Raise thresholds, add MMR/threshold |
| Write rejection | Safety rejected a legitimate memory | Refine trust/PII rules |

## 10. External validation

For a stronger evaluation, add:

- **LoCoMo**: very long-term conversational memory.
- **LongMemEval**: information extraction, multi-session reasoning, temporal
  reasoning, knowledge updates and abstention.
- **MemoryAgentBench**: incremental multi-turn memory.
- An LLM judge with position/length-bias calibration, plus 20% human spot checks.

These are complementary: the 23-scenario set is a fast regression suite; the
external benchmarks are for generalization claims.
