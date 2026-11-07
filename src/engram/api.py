"""FastAPI surface for Engram."""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from .config import MemoryConfig
from .debug import CounterfactualDebugger
from .errors import MemoryNotFound, SafetyViolation, TenantIsolationError
from .models import (
    ConflictRecord,
    ExplainRequest,
    ExtractRequest,
    MemoryQuery,
    MemoryRecord,
    MemorySearchResult,
    MemoryUpdateRequest,
    MemoryWriteRequest,
)
from .service import MemoryService
from .viewer import render_explain_html, render_trace_html


def create_app(config: MemoryConfig | None = None) -> FastAPI:
    service = MemoryService(config)
    if service.config.require_auth and not service.config.api_key:
        raise RuntimeError(
            "MEMORY_API_KEY must be set when authentication is required. "
            "Generate one with `python -c \"import secrets; "
            "print(secrets.token_urlsafe(24))\"`, or set "
            "MEMORY_REQUIRE_AUTH=false for a loopback-only development run."
        )
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            service.close()

    app = FastAPI(
        title="Engram",
        version="0.1.0",
        description="A lifecycle-aware long-term memory layer for LLM agents.",
        lifespan=lifespan,
    )
    app.state.service = service

    if service.config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(service.config.cors_origins),
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-API-Key"],
        )

    @app.middleware("http")
    async def _api_key_middleware(request: Request, call_next: Any) -> Any:
        if service.config.api_key and request.url.path.startswith("/v1/"):
            supplied = request.headers.get("X-API-Key", "")
            if not hmac.compare_digest(supplied, service.config.api_key):
                return JSONResponse(
                    status_code=401,
                    content={"error": "unauthorized", "detail": "invalid API key"},
                )
        return await call_next(request)

    @app.exception_handler(SafetyViolation)
    async def _safety_handler(_request: Any, exc: SafetyViolation) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": "safety_violation", "detail": str(exc)},
        )

    @app.exception_handler(MemoryNotFound)
    async def _not_found_handler(_request: Any, exc: MemoryNotFound) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "detail": str(exc)},
        )

    @app.exception_handler(TenantIsolationError)
    async def _tenant_handler(
        _request: Any, exc: TenantIsolationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"error": "tenant_isolation", "detail": str(exc)},
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok"}

    @app.post("/v1/memories", response_model=MemoryRecord)
    def write_memory(request: MemoryWriteRequest) -> MemoryRecord:
        return service.write(request)

    @app.post("/v1/memories/extract", response_model=list[MemoryRecord])
    def extract_memories(request: ExtractRequest) -> list[MemoryRecord]:
        return service.extract_and_write(request)

    @app.post("/v1/memories/search", response_model=MemorySearchResult)
    def search_memories(query: MemoryQuery) -> MemorySearchResult:
        return service.search(query)

    @app.post("/v1/explain")
    def explain(request: ExplainRequest) -> dict[str, Any]:
        service.assert_tenant(request.tenant_id)
        explanation = CounterfactualDebugger(service, request.user_id).explain(
            request.query,
            top_k=request.top_k,
            token_budget=request.token_budget,
            method=request.method,
            samples=request.samples,
        )
        return explanation.to_dict()

    @app.get("/v1/explain/view", response_class=HTMLResponse)
    def explain_view(
        user_id: str,
        query: str,
        top_k: int = 5,
        token_budget: int = 500,
        method: str = "leave_one_out",
        samples: int = 12,
        tenant_id: str | None = None,
    ) -> str:
        service.assert_tenant(tenant_id)
        explanation = CounterfactualDebugger(service, user_id).explain(
            query,
            top_k=top_k,
            token_budget=token_budget,
            method=method,
            samples=samples,
        )
        return render_explain_html(explanation.to_dict())

    @app.patch("/v1/memories/{memory_id}", response_model=MemoryRecord)
    def update_memory(memory_id: str, request: MemoryUpdateRequest) -> MemoryRecord:
        return service.update(memory_id, request)

    @app.delete("/v1/memories/{memory_id}", status_code=204)
    def delete_memory(memory_id: str) -> None:
        service.delete(memory_id)

    @app.post("/v1/maintenance")
    def maintenance(prune: bool = False) -> dict[str, int]:
        return service.maintenance(prune=prune)

    @app.get("/v1/stats")
    def stats(user_id: str | None = None) -> dict[str, Any]:
        return service.stats(user_id)

    @app.get("/v1/conflicts", response_model=list[ConflictRecord])
    def conflicts(
        user_id: str | None = None,
        status: str | None = None,
    ) -> list[ConflictRecord]:
        return service.conflicts(user_id=user_id, status=status)

    @app.post("/v1/conflicts/{conflict_id}/resolve", response_model=ConflictRecord)
    def resolve_conflict(
        conflict_id: str,
        strategy: str = Query(pattern="^(keep_new|keep_old|keep_both)$"),
    ) -> ConflictRecord:
        conflict = service.resolve_conflict(conflict_id, strategy)
        if conflict is None:
            raise HTTPException(status_code=404, detail="conflict not found")
        return conflict

    @app.get("/v1/trace")
    def trace(limit: int = 100) -> list[dict[str, Any]]:
        return service.trace.read(limit=limit, tenant_id=service.tenant_id)

    @app.get("/v1/trace/view", response_class=HTMLResponse)
    def trace_view(limit: int = 200) -> str:
        return render_trace_html(
            service.trace.read(limit=limit, tenant_id=service.tenant_id)
        )

    @app.get("/v1/export")
    def export(user_id: str | None = None) -> list[dict[str, Any]]:
        return service.export(user_id)

    @app.post("/v1/import")
    def import_records(records: list[dict[str, Any]]) -> dict[str, int]:
        return {"imported": service.import_records(records)}

    @app.get("/v1/memories/{memory_id}/attestation")
    def memory_attestation(memory_id: str) -> dict[str, Any]:
        verdict = service.verify_record_attestation(memory_id)
        if verdict is None:
            raise HTTPException(status_code=404, detail="memory not found")
        return verdict

    @app.get("/v1/audit/head")
    def audit_head() -> dict[str, Any]:
        return service.audit_head()

    @app.get("/v1/audit/verify")
    def audit_verify() -> dict[str, Any]:
        return service.verify_audit()

    return app


def get_app() -> FastAPI:
    """Uvicorn factory: `uvicorn --factory engram.api:get_app`."""
    return create_app()
