# Architecture

## 1. Goals

`Engram` provides a small, inspectable memory layer that:

- persists durable facts, preferences, goals, constraints and decisions;
- retrieves them under a token budget;
- updates or supersedes them when the user changes their mind;
- forgets them when they expire or become harmful;
- records enough trace to debug why a memory was written, retrieved or rejected.

It is deliberately **not** a model-training project and **not** a chatbot. The
demo agent exists only to exercise the memory API.

## 2. Component map

```text
┌──────────────────────────────────────────────────────────────┐
│ Callers: FastAPI / CLI / Demo Agent / P0 Deep Research Agent │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ MemoryService (orchestration)                                │
│  write / extract_and_write / search / update / forget        │
│  maintenance / conflicts / export / import                   │
└───────┬───────────────┬───────────────┬───────────────┬──────┘
        ▼               ▼               ▼               ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ WriteGuard   │ │ Extractor    │ │ Retrieval    │ │ Lifecycle    │
│ safety/PII   │ │ LLM/heuristic│ │ BM25+vector  │ │ conflict/    │
│ provenance   │ │              │ │ +recency     │ │ decay/TTL    │
│ attestation  │ │              │ │ +MMR         │ │              │
└───────┬──────┘ └───────┬──────┘ └───────┬──────┘ └───────┬──────┘
        └────────────────┴───────────────┴───────────────┘
                               ▼
              ┌──────────────────────────────────┐
              │ ProvenanceAttestor (HMAC chain)  │
              │ Audit hash chain (per tenant)    │
              └──────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ StoreSession: one connection, one transaction per write      │
│ SQLiteMemoryStore: memories / conflicts / events             │
│   tenant_id + content_hash + memory_tokens (inverted index)  │
│   per-thread connection cache; WAL; hash-chained events      │
│ TraceLogger: buffered JSONL append-only trace                │
└──────────────────────────────────────────────────────────────┘
```

Hot path: retrieval uses `memory_tokens` as an inverted index to get a
candidate pool, scores it with a vectorized numpy cosine, and selects with a
vectorized MMR. Writes use one `BEGIN IMMEDIATE` transaction covering memory,
entity/token indexes, conflict updates and the audit event.

## 3. Data model

A memory record carries:

| Field | Purpose |
|---|---|
| `id`, `user_id` | identity and isolation |
| `type` | working / episodic / semantic / procedural |
| `content`, `summary` | the memory itself |
| `entities` | lightweight retrieval and conflict keys |
| `source_type`, `source` | provenance and trust |
| `created_at`, `updated_at` | audit |
| `valid_from`, `valid_to` | temporal validity |
| `confidence` | how sure the system is |
| `importance` | how valuable the memory is |
| `last_accessed`, `access_count` | usage signal |
| `ttl_seconds` | automatic expiry |
| `sensitivity` | public / personal / sensitive / secret |
| `supersedes`, `superseded_by` | version chain |
| `metadata` | free-form annotations |
| `embedding` | float32 vector for similarity |

SQLite schema:

```sql
memories(id, user_id, type, content, summary, entities, source_type, source,
         created_at, updated_at, valid_from, valid_to, confidence, importance,
         last_accessed, access_count, ttl_seconds, sensitivity, supersedes,
         superseded_by, metadata, embedding)

conflicts(id, user_id, existing_id, new_id, reason, status, created_at,
          resolved_at, resolution)

events(id, ts, event, memory_id, user_id, payload)
```

`entities`, `supersedes` and `metadata` are JSON-encoded; embeddings are
`float32` blobs.

## 4. Write path

```text
text
  │
  ├─ length normalization / truncation
  ├─ SafetyGuard.assess
  │     ├─ injection? → reject
  │     ├─ secret? → reject
  │     ├─ untrusted source + semantic/procedural? → reject
  │     └─ PII? → redact unless confirmed
  ├─ provenance: every derived_from id must exist for the same user
  │     ├─ propagate taint from parents
  │     └─ compute provenance_verified on the server
  ├─ extract candidates (LLM or heuristic)
  ├─ dedupe: exact normalized content or cosine ≥ threshold
  │     └─ if a candidate conflicts, do not merge; go to lifecycle
  ├─ insert MemoryRecord
  └─ LifecycleManager.on_write
        ├─ detect conflicts
        ├─ supersede when trusted + confident (or explicit change)
        └─ record ConflictRecord
```

The same policy runs on `POST /v1/memories`, `POST /v1/import` and
`PATCH /v1/memories/{id}`. Import validates every record before writing any of
them and cannot change an id's owner; a patch cannot promote an untrusted or
tainted record to durable memory, edit `taint`/`provenance_verified`, or lower
sensitivity.

### Why extraction is separate from writing

The extractor proposes; the service decides. This makes it possible to swap an
LLM extractor for a deterministic one, replay an extraction, or evaluate
extraction quality without touching persistence.

## 5. Read path

```text
query
  │
  ├─ candidate fetch (user, type, active, not sensitive by default)
  ├─ graph candidate expansion by query entities
  ├─ BM25 score over content + entities
  ├─ embedding cosine similarity
  ├─ relevance = 0.75 * cosine + 0.25 * (query_coverage * normalized BM25)
  ├─ graph_score = |query_entities ∩ record_entities| / |query_entities|
  ├─ recency = 0.5 ** (days / half_life)
  ├─ score = w_rel * relevance + w_rec * recency
  │          + w_imp * importance + w_conf * confidence
  │          + w_graph * graph_score
  ├─ MMR diversity selection
  ├─ token-budget packing (truncate first hit if necessary)
  ├─ touch last_accessed / access_count
  └─ formatted context block
```

The score weights are configurable and normalized at runtime. The context block
is explicitly labelled as **context, not instructions**, to reduce the chance of
memory content being treated as a command.

## 6. Lifecycle

### Conflict detection

The detector is heuristic and intentionally conservative:

- explicit change markers (`现在`, `改成`, `改用`, `更新`, `instead`, `now`, ...)
  plus a minimum similarity → `explicit_change`;
- negation mismatch → `negation_mismatch`;
- same value pattern (`目标是`, `偏好`, `决定`) with different values →
  `value_conflict`;
- near-duplicates are merged before this stage, unless the new content is a
  conflict.

### Supersede

A new memory supersedes an old one when:

- the new source is trusted; and
- the new confidence is at least the old confidence, **or** the conflict is an
  explicit change.

Supersede sets `old.valid_to = now` and `old.superseded_by = new.id`, and appends
`old.id` to `new.supersedes`.

### Forgetting

| Mechanism | Trigger | Effect |
|---|---|---|
| TTL | `created_at + ttl_seconds` | `valid_to` set |
| Decay | `importance *= exp(-lambda * age_days)` | low-importance records expire |
| Pruning | explicit `maintenance(prune=True)` | deletes expired/pruned records |
| User deletion | `delete` / `forget` | hard delete |

## 7. Safety

The guard runs on every write:

- **Trust**: `WEB`, `TOOL`, `EMAIL` and `OTHER_USER` content cannot write
  semantic/procedural memory by default; it can still write episodic/working
  memory.
- **Injection**: phrases such as “ignore previous instructions”, “reveal your
  system prompt”, `rm -rf`, and their Chinese equivalents are rejected.
- **Secrets**: API keys, private keys and `password=` patterns are rejected.
- **PII**: email, phone, ID card and bank card patterns are classified as
  sensitive; content is redactable; sensitive memories are excluded from
  retrieval by default (`include_sensitive=False`).
- **Provenance (opt-in)**: agent writes must declare `derived_from`; taint
  propagates from untrusted parents and blocks durable writes. With
  `MEMORY_ATTESTATION_SECRET`, the write is signed; verification recomputes the
  payload from stored state and recursively checks the parent chain. Forged,
  missing or tampered parents are rejected. See
  [ATTESTATION.md](ATTESTATION.md).
- **Tenant boundary**: every memory, conflict and event carries `tenant_id`;
  store queries are scoped by `(tenant_id, user_id)`, and a request for another
  tenant is rejected.
- **Audit chain**: every event binds `prev_hash` and `event_hash`; the chain is
  per tenant and verified with `GET /v1/audit/verify`.

This is a baseline, not a complete AI-safety system. HMAC is a shared-secret
MAC; Ed25519/transparency-log anchoring and consensus voting are future work.

## 8. Observability

Two independent logs:

1. **SQLite `events`**: durable, hash-chained audit of write/search/update/
   delete/merge/maintenance, queryable via `recent_events`; verification
   returns the first broken event id.
2. **JSONL trace**: append-only, one line per operation, with timestamps and
   structured fields; content is fingerprinted, not stored verbatim.

The trace is intentionally separate from the database so it can be shipped to a
log store without changing the memory schema.

## 9. Why SQLite and hash embeddings

- **SQLite**: zero-ops, durable, inspectable, easy to test; sufficient for a
  single-user memory layer and a benchmark.
- **Hash embeddings**: deterministic, offline, no API key; makes tests and the
  benchmark reproducible. It is semantically weak by design, and the config
  switches to an OpenAI-compatible embedding endpoint.
- **No framework dependency**: the agent loop and memory lifecycle are plain
  Python; frameworks change, the lifecycle does not.

## 10. Extension points

- `EmbeddingProvider`: add a local sentence-transformer or a different API.
- `LLMClient`: add any chat-completions-compatible provider.
- `SQLiteMemoryStore`: add a Postgres/pgvector backend with the same interface.
- `ConflictDetector`: swap in an LLM judge for semantic conflicts.
- `Retriever`: add graph traversal or learned reranking.
- `Benchmark`: add LLM-judge and human annotation protocols.
