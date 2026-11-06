# MemPoison Submission Format

`mempoison-0.1` submissions are validated by:

```bash
engram poison-submit submission.json
```

The canonical JSON shape:

```json
{
  "name": "example-team",
  "defense": "provenance+classifier",
  "dataset_version": "mempoison-0.1",
  "split": "full",
  "metrics": {
    "asr": 0.08,
    "ur": 0.91,
    "fpr": 0.04,
    "detection_rate": 0.92,
    "adaptive_asr": 0.25
  },
  "case_results": [],
  "notes": "Describe the defense and any deviations from the harness."
}
```

Rules:

- `dataset_version` must be exactly `mempoison-0.1`.
- `asr`, `ur`, `fpr` are required and between 0 and 1.
- Report `split`: `full`, `base`, `adaptive`, or `heldout`.
- If you use a different victim, say so in `notes`; do not compare it silently
  with the deterministic simulator.
- `case_results` is optional but recommended; include per-case `case_id`,
  `attack_success`, `utility_retained`.
- Do not report a vendor comparison unless both systems were run on the same
  split with the same victim.

Schema: `bench/poison/submission.schema.json`.
