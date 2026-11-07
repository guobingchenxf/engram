# Delivery Checklist

## Delivered (code complete, Stage 5 verified)

- [x] Pydantic models: memory types, records, queries, hits, conflicts, safety assessment.
- [x] SQLite store: memories, conflicts, audit events, export/import, stats.
- [x] Embeddings: deterministic hash provider + OpenAI-compatible provider.
- [x] LLM: OpenAI-compatible chat client + robust JSON extraction.
- [x] Extraction: LLM path + deterministic heuristic fallback.
- [x] Retrieval: BM25 + embeddings + recency + importance + confidence, MMR, token budget, sensitive filtering, minimum relevance.
- [x] Lifecycle: conflict detection, explicit change, numeric/value/negation conflicts, supersede, TTL, decay, pruning, conflict resolution.
- [x] Safety: trust policy, prompt-injection patterns, secret rejection, PII classification/redaction, confirmation flag.
- [x] Service facade: write, extract_and_write, search, build_context, update, delete, forget, maintenance, conflicts, export/import.
- [x] FastAPI: health, write, extract, search, update, delete, maintenance, stats, conflicts, trace, export/import.
- [x] CLI: init, write, search, extract, stats, maintenance, conflicts, resolve, demo, bench.
- [x] Demo agent: five-session script exercising preference change and recall.
- [x] Benchmark: 23 scenarios across seven categories, memory vs full-history baseline, aggregate metrics, Markdown report.
- [x] Tests: models, storage, extraction, retrieval, lifecycle, safety, service, API, benchmark.
- [x] Docs: README, architecture, API, evaluation, design decisions.
- [x] Packaging: pyproject, requirements, Dockerfile, docker-compose, Makefile, .env.example.
- [x] Examples: quickstart, demo sessions, standalone benchmark script.

## Stage 1 verified (2026-11-05)

- [x] `uv venv --python 3.10 .venv` and `uv pip install -e ".[dev]"`.
- [x] `pytest` — 30 tests passed.
- [x] `ruff check src tests` — clean.
- [x] Multi-session demo works: preference change supersedes the old memory and
  records a conflict.
- [x] FastAPI smoke test: `/health`, write, search and stats return 200.

## Stage 2 verified (2026-11-05)

- [x] First benchmark run on the original 20-scenario set: memory 19/20,
  full-history 5/20.
- [x] Failure triage: `salary_conflict` failed because salary is classified as
  sensitive and excluded by default. Added an explicit `include_sensitive`
  check and re-ran.
- [x] Base-set result: memory **20/20**, full-history **5/20**.
- [x] Base-set ablations: `no_conflicts` 10/20, `no_sensitivity_filter` 18/20;
  `vector_only` and `no_rerank` 20/20 (retrieval ranking not stressed).
- [x] `reports/benchmark.md` contains real, reproducible numbers.
- [x] README and EVALUATION.md updated with results and caveats.

## Stage 2.5 hard retrieval verified (2026-11-05)

- [x] Added three distractor-heavy scenarios: project deadlines, metric table,
  preference domains.
- [x] First hard run: memory 20/23; all hard scenarios failed because different
  subjects were treated as contradictions.
- [x] Fixed subject/entity-aware and attribute-aware conflict detection; hard
  checks use `top_k=1` to test precision.
- [x] Final benchmark: memory **23/23**, full-history **5/23**.
- [x] Ablations: `no_conflicts` 13/23, `no_sensitivity_filter` 21/23;
  `vector_only` and `no_rerank` 23/23 (hash embeddings are lexical; retrieval
  ranking still not differentiated).

## Stage 3 verified (2026-11-05)

- [x] CI workflow: `.github/workflows/ci.yml` (pytest + ruff + mypy on Python
  3.10/3.11; Docker build + container smoke test).
- [x] Local equivalent: `ruff` clean, `mypy` clean, **33 tests passed**.
- [x] Docker image builds; container runs as non-root UID 10001.
- [x] `docker compose up --build`: healthcheck passes, write/search return 200.
- [x] API key auth: `/health` public; `/v1/*` returns 401 without `X-API-Key`
  and 200 with it.
- [x] LLM/embedding clients retry 429/5xx with exponential backoff (tested with
  `httpx.MockTransport`).
- [x] Wheel and sdist built; wheel installs in a clean venv, imports, writes,
  searches, and runs the CLI.

## Stage 4 verified (2026-11-05)

- [x] Graph memory: entity index, graph candidate expansion, graph score,
  `MEMORY_GRAPH_WEIGHT`, graph tests.
- [x] Red-team suite: 12 cases, **12/12 pass**.
- [x] LLM judge: rubric, heuristic fallback, position-swap and length-bias probes.
- [x] Trace viewer: HTML page + `GET /v1/trace/view` + CLI `trace-view`.
- [x] Benchmark dashboard: static HTML with condition bars, category bars,
  scenario grid and failures (`reports/benchmark.html`).
- [x] Deep Research integration: `MemoryToolbox` + `DeepResearchAgent` +
  `examples/deep_research_memory.py`.
- [x] `ruff` clean, `mypy` clean, **49 tests passed**.
- [x] Benchmark still **23/23**; red-team **12/12**.

## Trustworthy Memory / MemPoison verified (2026-11-05)

- [x] Repositioned Engram around **Trustworthy Memory** rather than
  "another lifecycle memory layer"; see `docs/POSITIONING.md`.
- [x] MemPoison dataset: **120 cases** = 108 base (4 channels × 3 strategies ×
  3 goals × 3 variants) + 12 adaptive, including 48 trust-laundering cases.
- [x] Harness: 5 defenses (`none`, `source_trust`, `classifier`, `isolation`,
  `provenance`); ASR / adaptive ASR / UR / FPR / detection latency.
- [x] Leaderboard: none ASR 100%, source_trust 40%, classifier 5% with FPR 25%,
  isolation 40%, provenance 10%; adaptive split defeats all five.
- [x] `reports/poison.md` + `docs/POISON.md` + `bench/poison/cases.jsonl`.
- [x] Counterfactual debugging: leave-one-out + sampled Shapley +
  fidelity@1/@2; `engram explain`.
- [x] LLM victim adapter (`--victim llm`), held-out split + annotation template,
  and `mempoison-0.1` submission schema/validator.
- [x] `ruff` clean, `mypy` clean, **80 tests passed**.

## Security hardening (2026-11-06)

Eight reproduced defects were fixed; each has a regression test in
`tests/test_security_regressions.py`.

- [x] `/v1/import` runs the full write policy, is all-or-nothing, and cannot
  change the owner of an existing id.
- [x] `provenance` rejects any unresolvable `derived_from` id; verified status
  is server-computed and cannot be written through metadata or a patch.
- [x] `PATCH` cannot promote an untrusted/tainted record to durable memory,
  edit protected metadata, or lower sensitivity.
- [x] Context rendering flattens content and puts the source in a structured
  prefix; isolation filters records by source, not by text lines.
- [x] `keep_old` / `keep_both` restore the older record instead of leaving both
  inactive.
- [x] `explicit_change` runs after subject/attribute checks and needs real
  overlap, so `现在` + weak similarity no longer supersedes unrelated memory.
- [x] Retrieval, dedupe and conflict detection scan all candidate rows up to a
  reported cap; the 64/250 windows are gone.
- [x] PII is redacted unless `confirmed=true`; traces store fingerprints, not
  payloads; the API requires a key by default; CORS is opt-in; compose binds to
  loopback.

## Not done (requires external validation / real environment)

- [ ] Run the LLM victim with a real provider.
- [ ] Human labels for the held-out split + inter-annotator agreement.
- [ ] Integrity checks/signatures and consensus voting as MemPoison defenses.
- [ ] Real embedding provider + redundancy-heavy scenarios to differentiate
  `vector_only` / `no_rerank`.
- [ ] TTL/time-controlled `no_forget` ablation.
- [ ] Real OpenAI-compatible embedding/LLM provider smoke test (mock only so far).
- [ ] LLM judge calibration on a real judge + human spot checks.
- [ ] External benchmark validation (LoCoMo, LongMemEval, MemoryAgentBench).
- [ ] Multi-user/Postgres/pgvector backend.
- [ ] Encryption at rest and a confirmation UI.
- [ ] Rate limiting / quotas (auth exists; throttling is roadmap).

## What to run first

```bash
cd D:\engram
pip install -e ".[dev]"
pytest -q
python -m engram.cli demo
python -m engram.cli poison --out reports/poison.md
python -m engram.cli bench --out reports/benchmark.md --html reports/benchmark.html
python -m engram.cli explain --user-id u1 --query "我的语言偏好"
```

If `pytest` fails, fix the first failure before running the benchmark. Do not
put benchmark numbers on a resume until the suite is green and the failures in
`reports/benchmark.md` have been reviewed.

## Definition of done for the portfolio

- [x] Tests green.
- [x] Benchmark report generated and reproducible.
- [x] At least one failure reviewed and fixed with a before/after metric.
- [x] README includes the actual result table, not placeholders.
- [x] A 3-minute demo shows preference change and cross-session recall.
- [x] MemPoison benchmark + leaderboard + technical report.
- [x] Counterfactual memory explanation (`engram explain`).
- [ ] A 1-page technical report explains architecture, baselines, ablations and limitations.

## Production write path (2026-11-06)

- [x] One guard for `write`, `patch` and `import`; no caller-supplied
  `taint`/`provenance_verified`/attestation is trusted blindly.
- [x] HMAC-SHA256 signed provenance over tenant/user/content/type/source/
  sensitivity/parent hashes; recursive verification with cycle/depth limits.
- [x] Attestation survives export/import only when valid; a tampered signature
  is rejected; unknown/expired keys are not trusted.
- [x] Tenant-scoped storage and API; cross-tenant reads, mutations and imports
  are rejected with `403 tenant_isolation`.
- [x] Per-tenant audit hash chain; payload tampering, deletion and prev-hash
  editing are detected with the first bad event id.
- [x] Legacy schema migration adds tenant/audit columns in place.
- [x] Negative tests: forged/missing/cross-tenant parent, tampered content,
  tampered attestation, invalid import, cross-tenant overwrite, audit tamper,
  re-attestation and auth/CORS defaults.
- [x] **115 tests**, ruff and mypy clean.

## Engineering depth pass (2026-11-07)

- [x] `StoreSession` unit-of-work: one connection / one `BEGIN IMMEDIATE` per
  write, covering memory, entity/token indexes, conflict updates and audit.
- [x] Per-thread connection cache; pragmas set once; explicit `close()` and
  FastAPI lifespan shutdown.
- [x] `content_hash` exact dedupe + `memory_tokens` inverted index for
  retrieval/dedupe/conflict candidate prefiltering; no full-corpus scans.
- [x] Vectorized cosine and vectorized MMR; cached tokenization/embeddings.
- [x] SQL stats, batched forget, one-transaction maintenance with idempotent
  decay (`last_decayed_at`).
- [x] `PRAGMA user_version` schema migration; full backfills only on version
  bump.
- [x] Per-user write locks and `idempotency_key` for safe retries.
- [x] `WriteGuard` extracted from `MemoryService`; storage split into
  `SQLiteMemoryStore` + `StoreSession`.
- [x] Measured at 1k memories: ~7ms/write (was ~241ms), ~50ms search
  (was ~979ms), ~1.5ms stats.
