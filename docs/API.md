# API Reference

Base URL: `http://127.0.0.1:8000`

All request and response bodies are JSON. The API is intentionally small; the
Python package is the primary interface and the HTTP layer mirrors it.

## Health

### `GET /health`

```json
{"status": "ok", "db_path": "./data/memory.db"}
```

## Write a memory

### `POST /v1/memories`

```json
{
  "user_id": "u1",
  "content": "我偏好中文解释，记住。",
  "type": "semantic",
  "source_type": "user",
  "source": "chat",
  "entities": ["中文"],
  "confidence": 0.85,
  "importance": 0.8,
  "ttl_seconds": null,
  "sensitivity": null,
  "metadata": {}
}
```

`type`: `working | episodic | semantic | procedural`
`source_type`: `user | agent | tool | web | email | other_user | system`

Response: a `MemoryRecord`.

A write can be rejected with `400 safety_violation` when it contains a secret,
a prompt-injection pattern, or an untrusted source tries to write durable
semantic/procedural memory.

## Extract memories from text

### `POST /v1/memories/extract`

```json
{
  "user_id": "u1",
  "text": "我的目标是 2028 届 Agent 实习。我偏好中文解释。",
  "source_type": "user",
  "source": "chat",
  "auto_write": true
}
```

Response: an array of written `MemoryRecord`s. If `auto_write` is false, the
endpoint returns an empty array; use the Python `service.extract()` to inspect
candidates without writing.

## Search memories

### `POST /v1/memories/search`

```json
{
  "user_id": "u1",
  "query": "我的语言偏好是什么",
  "types": null,
  "top_k": 8,
  "token_budget": 800,
  "include_expired": false,
  "include_sensitive": false,
  "exclude_ids": [],
  "time_range": null,
  "min_relevance": 0.15,
  "min_confidence": 0.0,
  "min_importance": 0.0
}
```

Response:

```json
{
  "hits": [
    {
      "memory": {"id": "...", "content": "...", "type": "semantic"},
      "score": 0.82,
      "relevance": 0.91,
      "recency": 0.98,
      "importance": 0.8,
      "confidence": 0.85,
      "graph": 0.0,
      "token_cost": 28
    }
  ],
  "context": "Relevant long-term memories ...",
  "total_tokens": 28,
  "candidates": 3,
  "truncated": false,
  "query": "我的语言偏好是什么"
}
```

Context lines are one per record and self-describing:

```text
- [semantic | 2026-11-06 | confidence=0.85 | source=user] 我偏好中文解释。
```

Content is flattened to a single line before rendering; the source is a
structured prefix, not a suffix that content could imitate. `truncated` is true
when the scan cap was reached, so a partial candidate set is visible instead of
silent.

`sensitive` and `secret` memories are excluded unless `include_sensitive` is
true. Expired and superseded memories are excluded unless `include_expired` is
true.

## Explain memory influence

### `POST /v1/explain`

```json
{
  "user_id": "u1",
  "query": "我的语言偏好",
  "top_k": 5,
  "token_budget": 500,
  "method": "leave_one_out",
  "samples": 12
}
```

`method`: `leave_one_out` or `shapley`. Runs counterfactual attribution and
returns `base_answer`, `hits`, `fidelity`, and an `influences` array.

### `GET /v1/explain/view?user_id=u1&query=我的语言偏好&method=shapley`

HTML version of the same explanation, for demos and debugging. `method` and
`samples` are optional.

## Update a memory

### `PATCH /v1/memories/{memory_id}`

```json
{
  "content": "我偏好英文解释。",
  "confidence": 0.9,
  "importance": 0.85,
  "sensitivity": "personal"
}
```

Only explicitly provided fields are applied. Changing `content` re-embeds the
record and re-runs safety checks.

## Delete a memory

### `DELETE /v1/memories/{memory_id}`

Returns `204 No Content`. Missing ids return `404 not_found`.

## Maintenance

### `POST /v1/maintenance?prune=false`

Runs expiry and decay. With `prune=true`, deletes expired/pruned records.

```json
{"expired": 2, "decayed": 5, "pruned": 0}
```

## Statistics

### `GET /v1/stats?user_id=u1`

```json
{
  "user_id": "u1",
  "total": 12,
  "active": 10,
  "expired": 2,
  "conflicts_open": 1,
  "by_type": {"semantic": 8, "episodic": 3, "working": 1}
}
```

## Conflicts

### `GET /v1/conflicts?user_id=u1&status=open`

Returns an array of `ConflictRecord`:

```json
{
  "id": "...",
  "user_id": "u1",
  "existing_id": "...",
  "new_id": "...",
  "reason": "explicit_change",
  "status": "open",
  "created_at": "...",
  "resolved_at": null,
  "resolution": null
}
```

### `POST /v1/conflicts/{conflict_id}/resolve?strategy=keep_new`

`strategy`: `keep_new | keep_old | keep_both`.

## Trace

### `GET /v1/trace?limit=100`

Returns the last N JSONL trace records for the service tenant. This is the
fastest way to see why a memory was written, merged, rejected or retrieved.

### `GET /v1/trace/view?limit=200`

Returns a self-contained HTML trace viewer (filterable table + raw payloads).
It is protected by the API key like every other `/v1/*` endpoint. Trace records
contain content fingerprints and lengths, never memory payloads.

## Export / import

### `GET /v1/export?user_id=u1`

Returns an array of memory records (including expired/superseded ones).

### `POST /v1/import`

Body: an array of exported records. Returns `{"imported": N}`.

Import is a write path, not a backdoor:

- every record is validated in full before any record is written
  (all-or-nothing);
- an existing id cannot change owner (`import_cross_user_overwrite`);
- safety, sensitivity and provenance checks run exactly as for `POST /v1/memories`;
- every `derived_from` parent must exist for the same tenant/user, either in the
  store or earlier in the same import batch;
- a record carrying a valid attestation keeps it; an invalid/tampered
  attestation is rejected;
- sensitive content is redacted on import; secrets are rejected;
- records from another tenant are rejected with `403 tenant_isolation`.

## Tenant isolation

Every request is scoped to the service tenant (`MEMORY_TENANT_ID`). A request
body may include `tenant_id`; it must equal the service tenant or the request
fails with `403 tenant_isolation`. The same applies to `update`, `delete`,
`search`, `export` and `import`. There is no cross-tenant read or write path.

## Signed provenance

Writes can carry a signed attestation (or the service can sign them with
`MEMORY_ATTESTATION_SECRET`). Verification is recursive over the parent chain.

### `GET /v1/memories/{memory_id}/attestation`

Returns the attestation verdict:

```json
{
  "status": "verified",
  "reasons": [],
  "key_id": "local",
  "issuer": "engram-service",
  "depth": 1,
  "root_ids": ["..."],
  "attestation_hash": "..."
}
```

Statuses include `verified`, `missing`, `invalid_signature`,
`invalid_issued_at`, `unknown_key`, `key_not_allowed`, `parent_missing`,
`parent_unverified`, `parent_changed`, `cycle`, `chain_too_deep` and
`disabled`.

## Audit chain

Every event is appended to a per-tenant SHA-256 hash chain.

### `GET /v1/audit/head`

```json
{"tenant_id": "default", "count": 42, "head_hash": "...", "last_event_id": 42, "last_event_at": "..."}
```

### `GET /v1/audit/verify`

```json
{"ok": true, "checked": 42, "tenant_id": "default", "head_hash": "...", "first_bad_id": null, "reason": null}
```

Deleting, editing or reordering an event yields `ok: false` with
`first_bad_id` and a reason (`event_hash_mismatch`, `prev_hash_mismatch`).

## Authentication

Authentication is required by default. Starting the app without
`MEMORY_API_KEY` raises at startup; set `MEMORY_REQUIRE_AUTH=false` only for a
loopback-only development run. Every `/v1/*` endpoint then requires:

```http
X-API-Key: <MEMORY_API_KEY>
```

`GET /health` is public. Missing or invalid keys return `401 unauthorized`.

## Errors

| Status | Error | Meaning |
|---|---|---|
| 400 | `safety_violation` | injection, secret, or untrusted durable write |
| 401 | `unauthorized` | missing or invalid `X-API-Key` |
| 403 | `tenant_isolation` | request crosses the service tenant boundary |
| 404 | `not_found` | memory or conflict id does not exist |
| 422 | validation error | malformed JSON or invalid enum/range |
| 500 | internal error | unexpected server error |

## CORS

CORS is opt-in. With `MEMORY_CORS_ORIGINS` empty the app sends no CORS headers;
set it to a comma-separated allowlist of application origins. There is no
wildcard default.

## Versioning

The current API is `v1`. Breaking changes will be introduced under a new
prefix rather than silently changing `v1`.
