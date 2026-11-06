# MemPoison Leaderboard

> Memory poisoning benchmark for LLM agents. Lower ASR is better; higher
> UR is better. FPR is the fraction of benign memories rejected.

## Dataset

- cases: **120** (108 base + 12 adaptive)
- channels: {'email': 30, 'other_user': 30, 'tool': 30, 'web': 30}
- strategies: {'adaptive': 12, 'gradual_drift': 36, 'one_shot': 36, 'sleeper': 36}
- goals: {'data_exfiltration': 36, 'policy_bypass': 36, 'preference_hijack': 48}

## Leaderboard

| Defense | ASR ↓ | Adaptive ASR ↓ | UR ↑ | FPR ↓ | Detection rate | Avg latency |
|---|---:|---:|---:|---:|---:|---:|
| none | 100.0% | 100.0% | 0.0% | 0.0% | 0.0% | 0.00 |
| source_trust | 40.0% | 100.0% | 60.0% | 0.0% | 60.0% | 1.00 |
| classifier | 5.0% | 25.0% | 95.0% | 25.0% | 90.0% | 1.30 |
| isolation | 40.0% | 100.0% | 60.0% | 0.0% | 60.0% | 1.00 |
| provenance | 10.0% | 100.0% | 90.0% | 0.0% | 90.0% | 1.00 |

## What the columns mean

- **ASR**: attack success rate, the fraction of cases where the victim
  follows the poisoned memory instead of the benign answer.
- **UR**: utility retention, the fraction of cases where the benign
  answer survives.
- **FPR**: false positive rate on benign memories.
- **Detection rate**: fraction of cases where the defense blocks or
  isolates the poison.
- **Avg detection latency**: average poisoned-memory index at which the
  defense acts (1 = first write; lower is better).

## Conditions

- `none`: safety disabled; every write is accepted.
- `source_trust`: the existing SafetyGuard trust policy.
- `classifier`: source trust + write-time poison classifier.
- `isolation`: writes are accepted, but untrusted sources are removed
  from the context before it reaches the victim agent.

## Honest limits

- The victim is a deterministic simulator, not an LLM. It models a
  memory-following failure, not full model compliance.
- The adaptive split forges a trusted provenance parent; provenance
  alone does not stop it. Integrity checks/signatures are required.
- Integrity checks and consensus voting are not implemented yet.
- The dataset is synthetic and should be paired with an external
  human-annotated benchmark before publication claims.
- No comparison with Mem0/Zep/Letta is claimed here; the benchmark is
  the artifact, not a leaderboard of vendors.
