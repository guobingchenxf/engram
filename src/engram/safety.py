"""Write-time safety checks for long-term memory.

Long-term memory is persistent, so a bad write is worse than a bad answer:
it can poison every future session. The guard enforces three rules:

1. untrusted sources cannot write durable semantic/procedural memory;
2. prompt-injection-like content is rejected;
3. PII and secrets are classified, optionally redacted, and may require
   confirmation before they are stored.
"""

from __future__ import annotations

import re

from .config import MemoryConfig
from .models import MemoryType, SafetyAssessment, Sensitivity, SourceType

_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, flags=re.IGNORECASE)
    for pattern in (
        r"\bignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?)\b",
        r"\bdisregard\s+(all\s+)?(previous|prior|above)\b",
        r"\breveal\s+(your\s+)?(instructions?|prompt|system\s+prompt)\b",
        r"\bsystem\s+prompt\b",
        r"\bexecute\s+(the\s+)?(following|this)\s+(command|code)\b",
        r"\brm\s+-rf\b",
        r"\bcurl\s+https?://",
        r"\byou\s+are\s+now\b",
        r"\bsend\b.{0,60}\b(api[_-]?key|password|token|secret)\b",
        r"\bgrant\s+(admin|root)\s+(access|privilege)",
        r"忽略(之前|以上|前面)的?(指令|提示|要求)",
        r"输出你的(指令|提示|系统提示)",
        r"执行以下(命令|代码|指令)",
        r"系统提示词?",
        r"你现在是",
        r"发送.{0,30}(密码|密钥|token)",
        r"授予.{0,20}(管理员|管理权限)",
    )
)

_POISON_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, flags=re.IGNORECASE)
    for pattern in (
        r"\b(always|must)\s+(recommend|output|say|return|answer)\b",
        r"\bignore\s+(all\s+)?(previous|safety|policy)\b",
        r"\badmin\s+policy\b",
        r"\bstore\s+this\s+permanently\b",
        r"\bsend\b.{0,80}(data|key|token|summar(?:y|ies)).{0,80}(to|https?://)",
        r"\bwhen\s+(the\s+user\s+)?ask(?:s|ed)?\b.{0,80}\b(say|output|recommend|send)\b",
        r"\bsilently\b",
        r"(总是|必须).{0,12}(推荐|输出|回答|说)",
        r"忽略.{0,12}(安全|策略|之前)",
        r"发送.{0,30}(数据|密钥|摘要).{0,20}(到|http)",
        r"当.{0,12}问.{0,20}(说|输出|推荐)",
    )
)

_PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone_cn": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "id_card_cn": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "bank_card": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
}

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, flags=re.IGNORECASE)
    for pattern in (
        r"\bsk-[A-Za-z0-9]{16,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"(api[_-]?key|secret|password|passwd|access[_-]?token)\s*[:=]\s*\S+",
        r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",
    )
)

_SENSITIVE_KEYWORDS = (
    "身份证",
    "银行卡",
    "手机号",
    "住址",
    "家庭地址",
    "病历",
    "密码",
    "工资",
    "薪资",
    "护照",
    "驾照",
)


class SafetyGuard:
    def __init__(self, config: MemoryConfig) -> None:
        self.config = config

    def assess(
        self,
        content: str,
        *,
        source_type: SourceType,
        requested_type: MemoryType,
        sensitivity_hint: Sensitivity | None = None,
    ) -> SafetyAssessment:
        reasons: list[str] = []
        detected = self.classify_sensitivity(content)
        sensitivity = _max_sensitivity(sensitivity_hint or detected, detected)

        if self.looks_like_injection(content):
            reasons.append("prompt_injection_pattern")

        if self.config.poison_classifier_enabled and self.looks_like_poison(content):
            reasons.append("poison_classifier")

        if self.contains_secret(content):
            sensitivity = Sensitivity.SECRET
            reasons.append("secret_material")

        if sensitivity == Sensitivity.SECRET and "secret_material" not in reasons:
            reasons.append("secret_material")

        if (
            not source_type.is_trusted
            and requested_type in {MemoryType.SEMANTIC, MemoryType.PROCEDURAL}
            and not self.config.allow_untrusted_semantic_write
        ):
            reasons.append("untrusted_source_cannot_write_durable_memory")

        allowed = not reasons
        requires_confirmation = (
            self.config.require_confirmation_for_sensitive
            and sensitivity in {Sensitivity.SENSITIVE, Sensitivity.SECRET}
        )

        return SafetyAssessment(
            allowed=allowed,
            reasons=reasons,
            sensitivity=sensitivity,
            redacted_content=self.redact(content) if sensitivity != Sensitivity.PUBLIC else None,
            requires_confirmation=requires_confirmation,
        )

    def classify_sensitivity(self, content: str) -> Sensitivity:
        if self.contains_secret(content):
            return Sensitivity.SECRET
        if self.contains_pii(content) or any(word in content for word in _SENSITIVE_KEYWORDS):
            return Sensitivity.SENSITIVE
        return Sensitivity.PERSONAL

    def looks_like_injection(self, content: str) -> bool:
        return any(pattern.search(content) for pattern in _INJECTION_PATTERNS)

    def looks_like_poison(self, content: str) -> bool:
        return any(pattern.search(content) for pattern in _POISON_PATTERNS)

    def contains_secret(self, content: str) -> bool:
        return any(pattern.search(content) for pattern in _SECRET_PATTERNS)

    def contains_pii(self, content: str) -> bool:
        return any(pattern.search(content) for pattern in _PII_PATTERNS.values())

    def redact(self, content: str) -> str:
        redacted = content
        for name, pattern in _PII_PATTERNS.items():
            redacted = pattern.sub(f"[REDACTED_{name.upper()}]", redacted)
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED_SECRET]", redacted)
        return redacted

    def is_untrusted(self, source_type: SourceType) -> bool:
        return not source_type.is_trusted


_SEVERITY = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.PERSONAL: 1,
    Sensitivity.SENSITIVE: 2,
    Sensitivity.SECRET: 3,
}


def _max_sensitivity(left: Sensitivity, right: Sensitivity) -> Sensitivity:
    return left if _SEVERITY[left] >= _SEVERITY[right] else right
