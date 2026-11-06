# Stage 3 Report: Engineering Usability

Date: 2026-11-05

## 1. CI

Added `.github/workflows/ci.yml`:

- Python 3.10 and 3.11 matrix;
- `pip install -e ".[dev]"`;
- `ruff check src tests`;
- `mypy src/engram`;
- `pytest -q`;
- Docker build and container smoke test (health endpoint).

Local equivalent run:

```text
ruff: All checks passed
mypy: Success: no issues found in 22 source files
pytest: 33 passed
```

## 2. Docker

```text
docker build -t engram:local .   # success
docker compose up -d --build           # success
curl /health                           # 200
POST /v1/memories                      # 200
POST /v1/memories/search               # 200
docker compose down                    # clean
```

Changes:

- container runs as non-root user UID 10001;
- compose healthcheck uses `urllib.request` against `/health`;
- Docker build includes the package from `src/`.

Note: on Linux, the bind-mounted `./data` directory must be writable by UID
10001, or use a named volume.

## 3. Packaging

```text
uv build --out-dir dist
# dist/engram-0.1.0-py3-none-any.whl (~51 KB)
# dist/engram-0.1.0.tar.gz
```

Clean-venv verification:

```text
uv venv --python 3.10 /tmp/am-wheel-venv
uv pip install --python ... dist/engram-0.1.0-py3-none-any.whl
import engram -> 0.1.0
write -> success
search -> 1 hit
python -m engram.cli init -> works
```

## 4. Auth and provider resilience

### API key

- `MEMORY_API_KEY` is optional.
- If set, `/v1/*` requires `X-API-Key`; `/health` stays public.
- Verified in Docker:
  - no key: `401`;
  - with key: `200`.

### Retries

- `OpenAICompatibleLLM` and `OpenAIEmbedding` retry HTTP 429/500/502/503/504
  with exponential backoff.
- Configured by `MEMORY_LLM_MAX_RETRIES` (default 2) and
  `MEMORY_LLM_RETRY_BACKOFF` (default 0.5s).
- Tests use `httpx.MockTransport`: 429 -> 200 and 503 -> 200 both pass.

## 5. Still missing

- real OpenAI-compatible provider smoke test (mock only);
- rate limiting / quotas (auth is implemented, throttling is roadmap);
- real embedding model to differentiate `vector_only` / `no_rerank`;
- TTL/time-controlled `no_forget` ablation;
- LLM judge and human spot checks;
- external benchmarks (LoCoMo, LongMemEval, MemoryAgentBench);
- Postgres/pgvector and encryption at rest.

## 6. Next

Stage 4 (depth/differentiation): graph memory, red-team suite, LLM judge,
external benchmark subset, and P0 Deep Research Agent integration.
