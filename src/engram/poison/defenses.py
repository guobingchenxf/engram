"""Defense conditions for MemPoison.

Six conditions are implemented as reference points:

* none: safety disabled; every write is accepted;
* source_trust: the existing SafetyGuard trust policy;
* classifier: source trust + write-time poison classifier;
* isolation: writes are accepted, but untrusted records are filtered out of
  retrieval by record source, not by matching rendered text;
* provenance: agent writes must resolve a trusted parent chain;
* attestation: source trust + HMAC-signed agent provenance. An agent write must
  carry a valid attestation, so a forged or missing parent cannot be laundered.

Integrity checks and consensus voting are natural extensions; they are not
implemented yet, and the report says so.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import MemoryConfig
from ..provenance import AttestationKey

_ATTESTATION_KEY = AttestationKey(
    key_id="mempoison-attestation",
    secret="mempoison-attestation-key-v1",
    issuer="mempoison-harness",
)


@dataclass(frozen=True)
class Defense:
    name: str
    safety_enabled: bool = True
    poison_classifier: bool = False
    isolate_untrusted: bool = False
    provenance: bool = False
    attestation: bool = False

    def configure(self, base: MemoryConfig) -> MemoryConfig:
        return base.with_overrides(
            safety_enabled=self.safety_enabled,
            poison_classifier_enabled=self.poison_classifier,
            provenance_enabled=self.provenance,
            retrieval_exclude_untrusted=self.isolate_untrusted,
            attestation_enabled=self.attestation,
            attestation_key_id=_ATTESTATION_KEY.key_id,
            attestation_secret=(
                _ATTESTATION_KEY.secret if self.attestation else None
            ),
            attestation_issuer=_ATTESTATION_KEY.issuer,
            trusted_attestation_keys=(
                (_ATTESTATION_KEY,) if self.attestation else ()
            ),
            require_attestation_for_agent=self.attestation,
            require_signed_parents=True,
        )

    def transform_context(self, context: str) -> str:
        # Retrieval already excludes untrusted records when isolation is on;
        # text-level filtering was bypassable with a newline.
        return context


DEFENSES: tuple[Defense, ...] = (
    Defense(name="none", safety_enabled=False),
    Defense(name="source_trust"),
    Defense(name="classifier", poison_classifier=True),
    Defense(name="isolation", safety_enabled=False, isolate_untrusted=True),
    Defense(name="provenance", safety_enabled=False, provenance=True),
    Defense(name="attestation", attestation=True),
)
