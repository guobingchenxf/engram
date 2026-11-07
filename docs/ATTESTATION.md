# Signed Provenance and Audit

This document describes the production write path: a single guard, signed
provenance attestations, tenant isolation and a tamper-evident audit chain.

## 1. Why "derived_from" is not provenance

The first version of Engram marked a write as `provenance_verified` when the
caller supplied a non-empty `derived_from` list. That is a claim, not evidence:
a caller could pass any id, or a forged parent, and the service would trust it.
The attestation layer replaces the claim with a signature over the stored state.

## 2. Attestation payload

Every signed record binds:

| Field | Why it is signed |
|---|---|
| `tenant_id`, `user_id`, `memory_id` | prevents moving a memory across tenants/users/ids |
| `content_sha256` | prevents content substitution after signing |
| `type`, `source_type`, `source`, `sensitivity` | prevents trust/type escalation |
| `created_at` | prevents backdating |
| `derived_from` + `parent_hashes` | binds the parent chain |
| `issuer`, `key_id` | identifies the signer and key |

The canonical payload is JSON with sorted keys and no insignificant whitespace.
The signature is HMAC-SHA256 over the canonical bytes, including the
server-generated `issued_at` (so a caller cannot backdate a signature to fit an
expired key). `attestation_hash` binds the signature envelope itself, so a child
can reference a parent attestation by hash.

## 3. Verification

`ProvenanceAttestor.verify()`:

1. looks up the `key_id` in the trust store;
2. checks the issuer, tenant/source allowlists and key validity window;
3. re-derives the payload from the **stored** record and **stored** parents;
4. compares the signature with `hmac.compare_digest`;
5. compares the content hash, parent hashes and attestation hash;
6. recursively verifies every parent, with cycle and depth limits.

A child whose parent's content, signature or existence changed fails with
`parent_unverified` / `parent_missing` / `invalid_signature`. An attestation
whose `key_id` is not trusted fails with `unknown_key`; an expired key fails with
`key_not_allowed`.

## 4. Key management

Generate a local key:

```bash
engram attest-keygen --key-id local --issuer engram-service
```

This prints a random secret and the matching environment variables. Set:

```text
MEMORY_ATTESTATION_ENABLED=true
MEMORY_ATTESTATION_KEY_ID=local
MEMORY_ATTESTATION_SECRET=<generated>
MEMORY_ATTESTATION_ISSUER=engram-service
```

The same key is used to sign writes accepted by the service and to verify them.
For an agent runtime with its own key, add its verification material to the
trust store:

```text
MEMORY_ATTESTATION_KEYS=[{"key_id":"agent-1","secret":"...","issuer":"agent-runtime","allowed_source_types":["agent"],"allowed_tenants":["tenant-a"]}]
MEMORY_REQUIRE_ATTESTATION_FOR_AGENT=true
```

`MEMORY_REQUIRE_ATTESTATION_FOR_AGENT=true` means an agent write must carry a
valid attestation; the service will not silently sign an unsigned agent claim.

## 5. Tenant isolation

Every memory, conflict and audit event carries a `tenant_id`. All store queries
are scoped by `(tenant_id, user_id)`; `update`, `delete`, `export`, `import`,
`stats` and `audit` are tenant-scoped. A request whose `tenant_id` differs from
the service tenant is rejected with `403 tenant_isolation`.

Legacy databases are migrated in place: `tenant_id` is added with the default
`default`, and per-tenant audit chains are backfilled once. Existing hashed rows
are never recomputed, because recomputing would silently heal tampering.

## 6. Audit chain

Every event is appended with:

```text
event_hash = SHA256(prev_hash + "\n" + canonical_event_json)
```

The chain is per tenant. Verification recomputes each row in `id` order and
returns the first bad id and a reason (`event_hash_mismatch`,
`prev_hash_mismatch`, `tenant_mismatch`, `invalid_payload_json`). Deleting,
editing or reordering an event breaks the chain.

API:

```text
GET /v1/audit/head
GET /v1/audit/verify
GET /v1/memories/{memory_id}/attestation
```

CLI:

```bash
engram audit-verify
engram attest-verify --memory-id <id>
```

## 7. Limits

- HMAC is a shared-secret MAC, not public-key non-repudiation. A verifier must
  hold the key. The `alg`/`key_id` fields are designed so an Ed25519 signer can
  replace HMAC without changing the record shape.
- The audit chain is tamper-evident, not tamper-proof: an attacker who can
  rewrite the whole database can recompute the chain. Ship the head hash to an
  external log or sign it with a key held outside the database for stronger
  guarantees.
- Tenant isolation is enforced in the application and SQL predicates; it is not
  row-level security in a multi-tenant database. Run one SQLite file per tenant
  for hard isolation, or move to Postgres with RLS.
- The service signs its own accepted writes. If the agent runtime is the
  issuer, configure its key in the trust store and require agent attestations.
