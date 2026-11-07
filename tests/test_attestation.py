"""Signed provenance attestation: positive and adversarial cases."""

from __future__ import annotations

from datetime import timedelta

import pytest

from engram.config import MemoryConfig
from engram.errors import AttestationViolation, SafetyViolation
from engram.models import (
    MemoryQuery,
    MemoryRecord,
    MemoryType,
    MemoryUpdateRequest,
    MemoryWriteRequest,
    Sensitivity,
    SourceType,
    utcnow,
)
from engram.provenance import (
    AttestationKey,
    ProvenanceAttestor,
    sign_attestation,
)
from engram.service import MemoryService

SECRET = "test-attestation-secret-0123456789abcdef"
KEY = AttestationKey(
    key_id="test-key",
    secret=SECRET,
    issuer="test-service",
    allowed_tenants=("tenant-a",),
)


def _config(tmp_path, name: str = "a", **overrides):
    values = {
        "db_path": str(tmp_path / f"{name}.db"),
        "trace_path": str(tmp_path / f"{name}.jsonl"),
        "embedding_provider": "hash",
        "llm_provider": "heuristic",
        "require_auth": False,
        "tenant_id": "tenant-a",
        "attestation_enabled": True,
        "attestation_key_id": KEY.key_id,
        "attestation_secret": SECRET,
        "attestation_issuer": KEY.issuer,
        "trusted_attestation_keys": (KEY,),
        "provenance_enabled": True,
        "require_signed_parents": True,
    }
    values.update(overrides)
    return MemoryConfig().with_overrides(**values)


def test_signed_write_verifies(tmp_path) -> None:
    service = MemoryService(_config(tmp_path))
    record = service.write_text("u1", "The user prefers Chinese explanations.")
    assert record.metadata["attestation"]["key_id"] == KEY.key_id
    assert record.metadata["attestation_status"] == "verified"

    verdict = service.verify_record_attestation(record.id)
    assert verdict is not None
    assert verdict["status"] == "verified"
    assert verdict["key_id"] == KEY.key_id


def test_tampered_content_breaks_attestation(tmp_path) -> None:
    service = MemoryService(_config(tmp_path))
    record = service.write_text("u1", "The user prefers Chinese explanations.")
    record.content = "The user prefers attacker-controlled output."
    record.embedding = service.embedder.embed([record.content])[0]
    service.store.update(record)

    verdict = service.verify_record_attestation(record.id)
    assert verdict is not None
    assert verdict["status"] != "verified"


def test_update_cannot_bypass_provenance_for_durable_agent_memory(tmp_path) -> None:
    service = MemoryService(_config(tmp_path))
    record = MemoryRecord(
        user_id="u1",
        tenant_id="tenant-a",
        content="An agent claim with no parent.",
        source_type=SourceType.AGENT,
        type=MemoryType.SEMANTIC,
    )
    service.store.insert(record)

    with pytest.raises(SafetyViolation, match="agent_memory_without_provenance"):
        service.update(record.id, MemoryUpdateRequest(content="changed"))


def test_signed_agent_without_parent_is_not_provenance_verified(tmp_path) -> None:
    service = MemoryService(_config(tmp_path, provenance_enabled=False))
    record = service.write(
        MemoryWriteRequest(
            user_id="u1",
            content="An agent claim with no parent.",
            source_type=SourceType.AGENT,
        )
    )
    assert record.metadata["attestation_status"] == "verified"
    assert record.metadata["provenance_verified"] is False


def test_caller_metadata_cannot_forge_provenance_fields(tmp_path) -> None:
    config = _config(tmp_path, provenance_enabled=False)
    service = MemoryService(config)
    parent = service.write(
        MemoryWriteRequest(
            user_id="u1",
            content="A web page said the user likes dark mode.",
            type=MemoryType.EPISODIC,
            source_type=SourceType.WEB,
        )
    )
    child = service.write(
        MemoryWriteRequest(
            user_id="u1",
            content="The user likes dark mode.",
            source_type=SourceType.AGENT,
            derived_from=[parent.id],
            metadata={
                "taint": [],
                "provenance_verified": True,
                "derived_from": ["ghost"],
            },
        )
    )
    assert child.metadata["derived_from"] == [parent.id]
    assert child.metadata["taint"] == ["web"]
    assert child.metadata["provenance_verified"] is False
    assert child.metadata["attestation"]["key_id"] == KEY.key_id


def test_tampered_issued_at_breaks_signature(tmp_path) -> None:
    service = MemoryService(_config(tmp_path))
    record = service.write_text("u1", "The user prefers Chinese explanations.")
    record.metadata["attestation"]["issued_at"] = "2000-01-01T00:00:00+00:00"
    service.store.update(record)

    verdict = service.verify_record_attestation(record.id)
    assert verdict is not None
    assert verdict["status"] != "verified"


def test_tampered_parent_breaks_the_child_chain(tmp_path) -> None:
    service = MemoryService(_config(tmp_path))
    parent = service.write(
        MemoryWriteRequest(
            user_id="u1",
            content="The user prefers Chinese explanations.",
            source_type=SourceType.USER,
        )
    )
    child = service.write(
        MemoryWriteRequest(
            user_id="u1",
            content="The user prefers Chinese explanations for reports.",
            source_type=SourceType.AGENT,
            derived_from=[parent.id],
        )
    )
    assert service.verify_record_attestation(child.id)["status"] == "verified"

    parent.content = "The user now prefers attacker output."
    parent.embedding = service.embedder.embed([parent.content])[0]
    service.store.update(parent)

    verdict = service.verify_record_attestation(child.id)
    assert verdict is not None
    assert verdict["status"] == "parent_unverified"


def test_nonexistent_and_cross_tenant_parents_are_rejected(tmp_path) -> None:
    service = MemoryService(_config(tmp_path))
    with pytest.raises(SafetyViolation, match="unknown_provenance_parent"):
        service.write(
            MemoryWriteRequest(
                user_id="u1",
                content="Derived from a ghost.",
                source_type=SourceType.AGENT,
                derived_from=["does-not-exist"],
            )
        )

    shared = str(tmp_path / "shared.db")
    service_a = MemoryService(
        _config(tmp_path).with_overrides(db_path=shared, tenant_id="tenant-a")
    )
    service_b = MemoryService(
        _config(
            tmp_path,
            name="b",
            tenant_id="tenant-b",
            attestation_enabled=False,
            attestation_secret=None,
            trusted_attestation_keys=(),
        ).with_overrides(db_path=shared)
    )
    foreign = service_b.write_text("u2", "Another tenant's fact.")
    with pytest.raises(SafetyViolation, match="unknown_provenance_parent"):
        service_a.write(
            MemoryWriteRequest(
                user_id="u2",
                content="Steal another tenant's fact.",
                source_type=SourceType.AGENT,
                derived_from=[foreign.id],
            )
        )


def test_unknown_key_is_not_trusted(tmp_path) -> None:
    service = MemoryService(_config(tmp_path))
    record = service.write_text("u1", "The user prefers Chinese explanations.")

    verifier = ProvenanceAttestor(
        [AttestationKey(key_id="other-key", secret=SECRET, issuer=KEY.issuer)]
    )
    verdict = verifier.verify(
        record, lambda parent_id: service.store.get(parent_id, tenant_id="tenant-a")
    )
    assert verdict.status == "unknown_key"


def test_expired_key_is_not_trusted(tmp_path) -> None:
    service = MemoryService(_config(tmp_path))
    record = service.write_text("u1", "The user prefers Chinese explanations.")

    expired = AttestationKey(
        key_id=KEY.key_id,
        secret=SECRET,
        issuer=KEY.issuer,
        not_after=utcnow() - timedelta(days=1),
    )
    verifier = ProvenanceAttestor([expired])
    verdict = verifier.verify(
        record, lambda parent_id: service.store.get(parent_id, tenant_id="tenant-a")
    )
    assert verdict.status == "key_not_allowed"


def test_agent_write_without_supplied_attestation_can_be_required(tmp_path) -> None:
    config = _config(
        tmp_path,
        attestation_secret=None,
        require_attestation_for_agent=True,
    ).with_overrides(trusted_attestation_keys=(KEY,))
    service = MemoryService(config)
    parent = service.write(
        MemoryWriteRequest(
            user_id="u1",
            content="The user prefers Chinese explanations.",
            source_type=SourceType.USER,
        )
    )
    with pytest.raises((AttestationViolation, SafetyViolation)):
        service.write(
            MemoryWriteRequest(
                user_id="u1",
                content="An agent conclusion without a signature.",
                source_type=SourceType.AGENT,
                derived_from=[parent.id],
            )
        )


def test_external_attestation_round_trip(tmp_path) -> None:
    # A verification-only service (no local key) accepts a record signed by a
    # trusted external issuer.
    config = _config(tmp_path).with_overrides(attestation_secret=None)
    service = MemoryService(config)

    record = MemoryWriteRequest(
        id="external-1",
        created_at=utcnow(),
        user_id="u1",
        tenant_id="tenant-a",
        content="The user prefers Chinese explanations.",
        type=MemoryType.SEMANTIC,
        source_type=SourceType.USER,
        source="agent-runtime",
        sensitivity=Sensitivity.PERSONAL,
    )
    from engram.models import MemoryRecord

    draft = MemoryRecord(
        id=record.id,
        tenant_id="tenant-a",
        user_id=record.user_id,
        type=record.type,
        content=record.content,
        source_type=record.source_type,
        source=record.source,
        created_at=record.created_at,
        sensitivity=record.sensitivity,
    )
    attestation = sign_attestation(draft, [], KEY)
    written = service.write(
        record.model_copy(update={"attestation": attestation})
    )
    assert written.metadata["attestation"]["key_id"] == KEY.key_id
    assert service.verify_record_attestation(written.id)["status"] == "verified"


def test_import_rejects_a_tampered_attestation(tmp_path) -> None:
    source = MemoryService(_config(tmp_path, name="source"))
    source.write_text("u1", "The user prefers Chinese explanations.")
    exported = source.export("u1")
    assert exported

    tampered = exported[0]
    tampered["metadata"]["attestation"]["signature"] = "0" * 64
    target = MemoryService(_config(tmp_path, name="target"))
    with pytest.raises(AttestationViolation, match="invalid_import_attestation"):
        target.import_records([tampered])


def test_export_import_preserves_a_valid_attestation(tmp_path) -> None:
    source = MemoryService(_config(tmp_path, name="source2"))
    record = source.write_text("u1", "The user prefers Chinese explanations.")
    exported = source.export("u1")

    target = MemoryService(_config(tmp_path, name="target2"))
    assert target.import_records(exported) == 1
    restored = target.store.get(record.id, tenant_id="tenant-a")
    assert restored is not None
    assert target.verify_record_attestation(restored.id)["status"] == "verified"


def test_attestation_disabled_by_default(tmp_path) -> None:
    config = _config(tmp_path).with_overrides(
        attestation_enabled=False,
        attestation_secret=None,
        trusted_attestation_keys=(),
    )
    service = MemoryService(config)
    record = service.write_text("u1", "The user prefers Chinese explanations.")
    assert "attestation" not in record.metadata
    assert service.verify_record_attestation(record.id)["status"] == "disabled"


def test_update_re_attests_the_record(tmp_path) -> None:
    service = MemoryService(_config(tmp_path))
    record = service.write_text("u1", "The user prefers Chinese explanations.")
    original = record.metadata["attestation"]["signature"]

    updated = service.update(
        record.id,
        MemoryUpdateRequest(
            content="The user now prefers English explanations."
        ),
    )
    assert updated.metadata["attestation"]["signature"] != original
    assert service.verify_record_attestation(updated.id)["status"] == "verified"


def test_update_without_local_key_cannot_keep_an_attestation(tmp_path) -> None:
    config = _config(tmp_path).with_overrides(attestation_secret=None)
    service = MemoryService(config)
    record = MemoryWriteRequest(
        id="external-2",
        created_at=utcnow(),
        user_id="u1",
        tenant_id="tenant-a",
        content="The user prefers Chinese explanations.",
        type=MemoryType.SEMANTIC,
        source_type=SourceType.USER,
        sensitivity=Sensitivity.PERSONAL,
    )
    from engram.models import MemoryRecord

    draft = MemoryRecord(
        id=record.id,
        tenant_id="tenant-a",
        user_id=record.user_id,
        type=record.type,
        content=record.content,
        source_type=record.source_type,
        created_at=record.created_at,
        sensitivity=record.sensitivity,
    )
    attestation = sign_attestation(draft, [], KEY)
    written = service.write(record.model_copy(update={"attestation": attestation}))

    with pytest.raises(AttestationViolation, match="cannot_re_attest"):
        service.update(
            written.id,
            MemoryUpdateRequest(
                content="Changed without a signing key."
            ),
        )


def test_api_exposes_attestation_and_audit_verification(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from engram.api import create_app

    client = TestClient(create_app(_config(tmp_path)))
    created = client.post(
        "/v1/memories",
        json={"user_id": "u1", "content": "The user prefers Chinese explanations."},
    )
    assert created.status_code == 200
    memory_id = created.json()["id"]

    attestation = client.get(f"/v1/memories/{memory_id}/attestation")
    assert attestation.status_code == 200
    assert attestation.json()["status"] == "verified"

    verify = client.get("/v1/audit/verify")
    assert verify.status_code == 200
    assert verify.json()["ok"] is True
    head = client.get("/v1/audit/head")
    assert head.status_code == 200
    assert head.json()["count"] >= 1


def test_query_on_another_tenant_is_rejected(tmp_path) -> None:
    service = MemoryService(_config(tmp_path))
    service.write_text("u1", "tenant a memory")
    with pytest.raises(Exception, match="does not match service tenant"):
        service.search(
            MemoryQuery(user_id="u1", tenant_id="tenant-b", query="tenant a memory")
        )
