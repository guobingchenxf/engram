# Design Decisions

This document records the choices that shaped `Engram`, the alternatives
considered, and the trade-offs that remain.

## 1. Memory is not RAG, and not a long context

| | Context window | RAG | Memory (this project) |
|---|---|---|---|
| Scope | current turn | external documents | the agent's own users, tasks and history |
| Lifetime | session | index lifetime | explicit, updatable, deletable |
| Write path | append everything | ingest documents | extract, classify, dedupe, conflict-check |
| Update | none | re-index | supersede, correct, forget |
| Privacy | hard to remove | hard to remove | sensitivity + deletion API |

A long context is expensive and dilutes attention; a vector store of chat logs
has no schema, no time model and no forgetting. Memory is a state lifecycle.

## 2. Why the write path is the hard part

Most projects spend their effort on retrieval and ignore what gets stored. In
practice, memory quality is decided at write time:

- if you store everything, retrieval noise grows and privacy risk explodes;
- if you store too little, the agent cannot personalize;
- if you store without confidence, a single inference becomes a permanent fact;
- if you store without provenance, you cannot audit or delete it.

The service therefore separates:

1. extraction (LLM or heuristic);
2. safety assessment;
3. dedupe/merge;
4. lifecycle/conflict;
5. persistence + trace.

## 3. Explicit vs implicit memory

The system carries `explicit` in `ExtractedMemory` and stores it in
`metadata["explicit"]`. The default heuristic extractor marks direct user
statements as explicit; an LLM extractor can mark inferred preferences as
implicit.

Why not a dedicated column yet?

- it keeps the first schema small and easy to migrate;
- `source_type`, `confidence` and `metadata["explicit"]` already carry enough
  signal for the current retrieval policy;
- a dedicated `explicit` column is a straightforward migration once the
  evaluation shows it matters.

Trade-off: querying by explicit/implicit currently requires JSON access or an
application-side filter. This is acceptable for a single-user system; a
multi-tenant system should promote it to a column.

## 4. Why SQLite

- zero-ops and durable;
- easy to inspect and back up;
- sufficient for a personal memory layer and a benchmark;
- no external service to fail during an interview demo.

Alternatives considered:

- **Postgres + pgvector**: better for multi-user and real vector search, but adds
  operational overhead. The store interface is designed to be replaceable.
- **Pure in-memory**: fastest to prototype, but loses the "persistent" property
  that is the whole point of memory.
- **Redis**: good for working memory and caching, not ideal as the source of
  truth for long-term records.

## 5. Why hash embeddings by default

The project must be runnable offline, in CI, and without an API key. Hash
embeddings are deterministic and dependency-free. They are semantically weak:
synonyms and paraphrases will not match well. The config supports an
OpenAI-compatible embedding endpoint for real use.

Trade-off: the benchmark measures the system and the lifecycle, not
state-of-the-art semantic retrieval. That is intentional: a memory layer's value
comes from lifecycle and evaluation, not from a particular embedding model.

## 6. Why hybrid retrieval

Pure vector search fails on:

- exact identifiers, numbers and dates;
- rare entities and Chinese terms;
- short queries with little semantic signal.

BM25 complements embeddings on exact matches; recency and importance prevent
old but semantically similar memories from dominating. MMR reduces redundancy
when several memories say the same thing.

The weights are configurable because the right balance depends on the domain. A
personal assistant should weight recency more; a research assistant may weight
importance and confidence more.

## 7. Conflict policy

The system does not silently overwrite. It:

1. detects a conflict;
2. records a `ConflictRecord`;
3. supersedes the old record only when the new write is trusted and either more
   confident or explicitly a change;
4. allows `keep_new`, `keep_old` or `keep_both` resolution.

Why not always prefer the newest? Because a user can be wrong, an agent can
mis-extract, and a tool can be stale. Keeping the conflict visible is safer than
losing information.

## 8. Forgetting

A memory that never forgets becomes a liability:

- stale preferences override current ones;
- storage and retrieval costs grow;
- deleted or sensitive data persists;
- old errors become permanent beliefs.

The project uses TTL, exponential decay and explicit deletion. The decay rate is
configurable; the default is deliberately slow.

## 9. Safety and trust

Long-term memory is a persistent attack surface. A single poisoned write can
affect every future session. The guard:

- rejects injection-like text;
- rejects secrets;
- prevents untrusted sources (web/tool) from writing durable semantic or
  procedural memory;
- classifies PII as sensitive and excludes it from retrieval by default;
- supports redaction for logs.

Trade-off: false positives are possible. A user quoting an injection example may
be rejected. This is acceptable for a first version; a production system should
add a confirmation flow and allowlists.

## 10. No agent framework dependency

The memory layer does not depend on LangChain, LangGraph or AutoGen. Those
frameworks change quickly and hide control flow. The logic here is plain Python
and can be plugged into any agent framework through the four API calls:
`write`, `search`, `update`, `delete`.

## 11. Evaluation before features

The 23-scenario benchmark exists so the project can answer:

- does memory beat a full-history baseline?
- which failure mode is still broken?
- did a change improve the system or just change it?

The benchmark is intentionally small and deterministic. It is a regression
suite, not a claim of general intelligence. External benchmarks (LoCoMo,
LongMemEval, MemoryAgentBench) and an LLM judge are the next step.

## 12. What would change at production scale

- Replace SQLite with Postgres + pgvector.
- Add tenant isolation and row-level security.
- Encrypt sensitive memory at rest.
- Add a confirmation UI for sensitive writes.
- Add rate limits, audit retention and GDPR-style deletion.
- Add distributed tracing and alerting.
- Add a memory poisoning red-team suite.
- Add a dedicated explicit/implicit column and a graph memory backend.
