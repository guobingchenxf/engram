"""Append-only JSONL trace used for debugging and evaluation."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def fingerprint(text: str) -> str:
    """A short, non-reversible stand-in for sensitive content in traces."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class TraceLogger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._handle: Any = None

    def _ensure_handle(self) -> Any:
        if self._handle is None:
            self._handle = self.path.open("a", encoding="utf-8")
        return self._handle

    def log(self, event: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            handle = self._ensure_handle()
            handle.write(line + "\n")
            handle.flush()

    def read(
        self, limit: int = 100, *, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            if self._handle is not None:
                self._handle.flush()
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        # When filtering, parse before applying the limit: the last N lines may
        # include another tenant.  When not filtering, keep the fast path.
        selected = lines[-limit:] if limit > 0 and tenant_id is None else lines
        records: list[dict[str, Any]] = []
        for line in selected:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if tenant_id is not None:
                record_tenant = str(record.get("tenant_id") or "default")
                if record_tenant != tenant_id:
                    continue
            records.append(record)
        return records[-limit:] if limit > 0 else records

    def clear(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            if self.path.exists():
                self.path.unlink()

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.flush()
                self._handle.close()
                self._handle = None
