# Positioning: Trustworthy Memory

## One sentence

**Engram is a trustworthy memory layer for LLM agents: it treats the write path
as a security and correctness boundary, and ships MemPoison, an open benchmark
for cross-session memory poisoning.**

## What Engram is not

- Not a vector database with a chat-history wrapper.
- Not a general-purpose RAG stack.
- Not a "better Mem0/Zep/Letta"; no vendor comparison is claimed.
- Not a rule-based Deep Research Agent. `DeepResearchAgent` is an integration
  example, not a product claim.

## The gap

The public positioning of Mem0, Zep and Letta centers on managed memory,
retrieval quality, temporal graphs and agent runtime integration. Engram's public
artifact is different:

| Axis | Common memory layer | Engram |
|---|---|---|
| Primary design goal | retrieval quality, integrations | write-path trust + correctness |
| Provenance | source metadata | trust boundary, trust laundering, per-write verdict |
| Update | upsert / overwrite | conflict detection + supersede + audit |
| Forgetting | TTL / delete | TTL, decay, pruning, deletion |
| Debugging | logs | counterfactual attribution (leave-one-out / Shapley) + fidelity@k |
| Evidence | vendor benchmarks | open MemPoison benchmark + reproducible results |
| Failure story | "retrieval missed" | "poisoned write persisted across sessions" |

## Why this is defensible

- **The write path is under-served.** Retrieval is crowded; a poisoned write is
  a persistent security failure, and most public memory demos do not model it.
- **The code already lives here.** `safety.py`, `lifecycle.py`, `trace.py`,
  `viewer.py` and `redteam.py` are the right primitives; MemPoison is the
  measurement layer, not a rewrite.
- **The evidence is an asset.** A benchmark others can run and cite is harder to
  dismiss than another feature list.

## What we do not do

- Do not add graph RAG, multi-agent orchestration or more integrations before
  the benchmark has external users.
- Do not claim "outperforms Mem0" without an external, held-out evaluation.
- Do not use the 23-scenario lifecycle suite as the primary evidence; it is a
  regression suite.
- Do not describe `deep_research.py` as a production Deep Research Agent.
- Do not optimize for stars; optimize for reproducible artifacts and feedback.

## Evidence hierarchy

1. **MemPoison** — 120 cases, 5 defenses, adaptive split, ASR/UR/FPR; see
   [POISON.md](POISON.md).
2. **Counterfactual debugging** — leave-one-out and sampled-Shapley influence,
   with fidelity@1/@2 so the attribution is checked, not asserted; see
   `engram explain`.
3. **Lifecycle benchmark** — 23 scenarios, memory 23/23 vs full-history 5/23;
   regression only.
4. **Red-team** — 12 cases, 12/12.

## Next milestones

- Add Ed25519/public verification, external audit-head anchoring and consensus
  voting on top of the signed attestation path.
- Run the LLM victim adapter (`--victim llm`) against a real provider.
- Have two humans label the held-out split and report agreement.
- Collect the first external submission in the published format.
- Publish the benchmark + reference implementation, not just the library.
