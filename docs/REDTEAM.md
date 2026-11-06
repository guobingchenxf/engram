# Red-Team Suite

The red-team suite tests the **memory write path**, not the LLM. Its goal is to
answer a narrow question: can untrusted, malicious, or sensitive content become
durable memory?

Run it:

```bash
python -m engram.cli redteam
```

Current result: **12/12 cases pass**.

## Cases

| ID | Category | Source | Expected |
|---|---|---|---|
| `injection_user_en` | prompt_injection | user | rejected |
| `injection_user_zh` | prompt_injection | user | rejected |
| `injection_web` | prompt_injection | web | rejected |
| `data_exfiltration` | data_exfiltration | user | rejected |
| `permission_abuse` | permission_abuse | user | rejected |
| `memory_poisoning_tool` | memory_poisoning | tool | rejected |
| `secret_key` | secret | user | rejected, `SECRET` |
| `pii_phone` | privacy | user | allowed, `SENSITIVE`, confirmation |
| `pii_email` | privacy | user | allowed, `SENSITIVE`, confirmation |
| `untrusted_episodic` | trust | web | allowed (episodic only) |
| `benign_preference` | benign | user | allowed, `PERSONAL` |
| `untrusted_semantic` | trust | web | rejected |

## What it catches

- **Prompt injection**: “ignore previous instructions”, “reveal your system
  prompt”, “you are now…”, and Chinese equivalents.
- **Data exfiltration**: “send the API key/password to …”.
- **Permission abuse**: “grant admin access…”.
- **Memory poisoning**: tool/web content trying to write durable semantic memory.
- **Secrets**: API keys and private-key blocks.
- **PII**: phone/email/ID/bank-card patterns are classified as sensitive and
  require confirmation; they are excluded from retrieval by default.

## Limitations

- This is a heuristic baseline, not a complete AI-safety evaluation.
- False positives are possible (for example, a quoted attack phrase).
- It does not test indirect injection through retrieved documents or tool output
  that is already in context; that requires an end-to-end agent test.
- The suite checks the guard, not whether a particular model would comply.

## Extending it

Add a `RedTeamCase` to `src/engram/redteam.py` with:

- `content`
- `source_type`
- `requested_type`
- `expect_allowed`
- optional `expect_sensitivity` / `expect_confirmation`

Then run the suite and the tests. A new case should fail before the guard is
updated and pass after it.
