"""Defense conditions for MemPoison.

Four conditions are implemented as reference points:

* none: safety disabled; every write is accepted;
* source_trust: the existing SafetyGuard trust policy;
* classifier: source trust + write-time poison classifier;
* isolation: writes are accepted, but untrusted sources are removed from the
  context before it reaches the victim agent.

Integrity checks and consensus voting are natural extensions; they are not
implemented yet, and the report says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import MemoryConfig

_CONTEXT_LINE = re.compile(
    r"^- \[.*?\] .*? \(source: (web|tool|email|other_user)\)$"
)


@dataclass(frozen=True)
class Defense:
    name: str
    safety_enabled: bool = True
    poison_classifier: bool = False
    isolate_untrusted: bool = False
    provenance: bool = False

    def configure(self, base: MemoryConfig) -> MemoryConfig:
        return base.with_overrides(
            safety_enabled=self.safety_enabled,
            poison_classifier_enabled=self.poison_classifier,
            provenance_enabled=self.provenance,
        )

    def transform_context(self, context: str) -> str:
        if not self.isolate_untrusted or not context:
            return context
        lines = []
        for line in context.splitlines():
            if _CONTEXT_LINE.match(line):
                continue
            lines.append(line)
        return "\n".join(lines)


DEFENSES: tuple[Defense, ...] = (
    Defense(name="none", safety_enabled=False),
    Defense(name="source_trust"),
    Defense(name="classifier", poison_classifier=True),
    Defense(name="isolation", safety_enabled=False, isolate_untrusted=True),
    Defense(name="provenance", safety_enabled=False, provenance=True),
)
