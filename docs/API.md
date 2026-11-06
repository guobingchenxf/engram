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
  "query": "我的语言偏好是什么"
}
```

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

Returns the last N JSONL trace records. This is the fastest way to see why a
memory was written, merged, rejected or retrieved.

### `GET /v1/trace/view?limit=200`

Returns a self-contained HTML trace viewer (filterable table + raw payloads).
It is protected by `X-API-Key` when `MEMORY_API_KEY` is set.

## Export / import

### `GET /v1/export?user_id=u1`

Returns an array of memory records (including expired/superseded ones).

### `POST /v1/import`

Body: an array of exported records. Returns `{"imported": N}`.

## Authentication

If `MEMORY_API_KEY` is set, every `/v1/*` endpoint requires the header:

```http
X-API-Key: <MEMORY_API_KEY>
```

`GET /health` is public. Missing or invalid keys return `401 unauthorized`.

## Errors

| Status | Error | Meaning |
|---|---|---|
| 400 | `safety_violation` | injection, secret, or untrusted durable write |
| 401 | `unauthorized` | missing or invalid `X-API-Key` |
| 404 | `not_found` | memory or conflict id does not exist |
| 422 | validation error | malformed JSON or invalid enum/range |
| 500 | internal error | unexpected server error |

## CORS

The development app allows all origins. Before deploying, restrict
`allow_origins` to your application domain.

## Versioning

The current API is `v1`. Breaking changes will be introduced under a new
prefix rather than silently changing `v1`.
