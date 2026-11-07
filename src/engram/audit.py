"""Tamper-evident audit chain for memory events.

Every audit event is appended with ``prev_hash`` and ``event_hash``:

    event_hash = SHA256(prev_hash + "\\n" + canonical_event_json)

The chain is per tenant.  Editing, deleting or re-ordering an event breaks
every later hash, so ``verify_chain`` can detect tampering after the fact.  A
hash chain is tamper-*evident*, not tamper-*proof*: an attacker who can rewrite
the whole database can recompute it.  Anchoring the head hash externally (log
shipping, transparency log, or a signature over the head) is the production
upgrade; ``sign_head`` in the service layer provides the first step.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

GENESIS_HASH = "0" * 64


def canonical_event(
    *,
    ts: str,
    event: str,
    memory_id: str | None,
    user_id: str | None,
    tenant_id: str,
    actor: str | None,
    payload: Mapping[str, Any],
) -> str:
    return json.dumps(
        {
            "ts": ts,
            "event": event,
            "memory_id": memory_id,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "actor": actor,
            "payload": dict(payload),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def chain_hash(prev_hash: str, body: str) -> str:
    return hashlib.sha256((prev_hash + "\n" + body).encode("utf-8")).hexdigest()


@dataclass
class AuditVerifyResult:
    ok: bool
    checked: int
    tenant_id: str
    head_hash: str
    first_bad_id: int | None = None
    reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "tenant_id": self.tenant_id,
            "head_hash": self.head_hash,
            "first_bad_id": self.first_bad_id,
            "reason": self.reason,
            **self.extra,
        }


def verify_chain(
    rows: Sequence[Mapping[str, Any]],
    *,
    tenant_id: str,
) -> AuditVerifyResult:
    """Verify rows ordered by ``id`` ascending.

    Rows must expose id, ts, event, memory_id, user_id, tenant_id, actor,
    payload, prev_hash and event_hash.  ``payload`` may be a JSON string or a
    mapping.
    """
    prev = GENESIS_HASH
    checked = 0
    for row in rows:
        row_tenant = str(row.get("tenant_id") or "default")
        if row_tenant != tenant_id:
            return AuditVerifyResult(
                False,
                checked,
                tenant_id,
                prev,
                first_bad_id=int(row["id"]),
                reason="tenant_mismatch",
            )
        stored_prev = str(row.get("prev_hash") or "")
        stored_hash = str(row.get("event_hash") or "")
        if not stored_prev and not stored_hash:
            # Legacy row created before the audit chain existed.  It is
            # immutable history; the chain starts at the first hashed row.
            checked += 1
            continue
        if stored_prev != prev:
            return AuditVerifyResult(
                False,
                checked,
                tenant_id,
                prev,
                first_bad_id=int(row["id"]),
                reason="prev_hash_mismatch",
            )
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return AuditVerifyResult(
                    False,
                    checked,
                    tenant_id,
                    prev,
                    first_bad_id=int(row["id"]),
                    reason="invalid_payload_json",
                )
        if not isinstance(payload, Mapping):
            return AuditVerifyResult(
                False,
                checked,
                tenant_id,
                prev,
                first_bad_id=int(row["id"]),
                reason="invalid_payload_type",
            )
        body = canonical_event(
            ts=str(row["ts"]),
            event=str(row["event"]),
            memory_id=row.get("memory_id"),
            user_id=row.get("user_id"),
            tenant_id=row_tenant,
            actor=row.get("actor"),
            payload=payload,
        )
        expected = chain_hash(stored_prev, body)
        if expected != stored_hash:
            return AuditVerifyResult(
                False,
                checked,
                tenant_id,
                prev,
                first_bad_id=int(row["id"]),
                reason="event_hash_mismatch",
            )
        prev = stored_hash
        checked += 1
    return AuditVerifyResult(True, checked, tenant_id, prev)
