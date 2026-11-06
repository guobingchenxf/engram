# Stage 5 Report: Trustworthy Memory + MemPoison

Date: 2026-11-05

## 1. The identity change

Engram is no longer positioned as "a lifecycle-aware memory layer". That
sentence is true of Mem0, Zep and Letta and gives no reason to look.

New one-liner:

> **Engram is a trustworthy memory layer: it treats the write path as a
> security and correctness boundary, and ships MemPoison, an open benchmark for
> cross-session memory poisoning.**

See [POSITIONING.md](POSITIONING.md).

## 2. MemPoison

120 cases: 108 base + 12 adaptive.

- 4 channels: web, tool, email, other user;
- 3 persistence strategies: one-shot, gradual drift, sleeper;
- 3 goals: preference hijack, policy bypass, data exfiltration;
- 3 variants; one third are trust-laundered (agent writes untrusted content as
  its own memory);
- 12 adaptive cases forge a trusted provenance parent and avoid obvious markers.

Harness: 5 defenses, ASR / adaptive ASR / UR / FPR / detection latency.

| Defense | ASR | Adaptive ASR | UR | FPR | Detection |
|---|---:|---:|---:|---:|---:|
| none | 100.0% | 100.0% | 0.0% | 0.0% | 0.0% |
| source_trust | 40.0% | 100.0% | 60.0% | 0.0% | 60.0% |
| classifier | **5.0%** | 25.0% | **95.0%** | 25.0% | 90.0% |
| isolation | 40.0% | 100.0% | 60.0% | 0.0% | 60.0% |
| provenance | **10.0%** | **100.0%** | 90.0% | 0.0% | 90.0% |

Key result: source trust alone is defeated by trust laundering; retrieval
isolation has the same blind spot; a write-time classifier is strongest but
rejects 25% of benign instruction-like memories; provenance is best on the base
set but fails every adaptive case because forged provenance is still
provenance. Integrity/signatures are the next defense.

Artifacts:

- `bench/poison/cases.jsonl` — 120 cases;
- `reports/poison.md` — leaderboard;
- `docs/POISON.md` — protocol, metrics, findings, limits;
- `engram poison` — reproduction command.

## 3. Counterfactual memory debugging

`engram explain --user-id u1 --query ...` performs counterfactual attribution:

- `leave_one_out`: remove each hit, re-run, and score whether the answer changes;
- `shapley`: sampled permutations, estimated marginal contribution per memory;
- `fidelity@1` / `fidelity@2`: remove the top-k attributed memories at once and
  check whether the answer actually changes.

This is the demo layer, not the benchmark. Full-exact Shapley and a real LLM
answerer are future work.

## 4. What was not done

- LLM victim adapter exists (`--victim llm`) but has not been run with a real
  provider.
- Held-out split and annotation CSV exist, but the labels are empty.
- Provenance handles base laundering but not forged provenance (adaptive split).
- Integrity/signatures and consensus voting as defenses.
- External comparison with Mem0/Zep/Letta.
- Real embedding provider smoke test.

## 5. Verification

```text
ruff:  All checks passed
mypy:  Success (37 source files)
pytest: 64 passed
MemPoison: 120 cases × 5 defenses
lifecycle benchmark: memory 23/23, full-history 5/23
red-team: 12/12
```

## 6. Next

Do not add features. The next milestone is external evidence:

1. LLM victim + human-annotated held-out split.
2. Open submission format and leaderboard.
3. Integrity/consensus defenses.
4. One external team running MemPoison and reporting results.
