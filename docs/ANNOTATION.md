# Held-Out Split and Annotation

`python -m engram.poison` writes:

- `bench/poison/heldout.jsonl` — a deterministic 20% split (24 of 120 cases,
  seed 7);
- `bench/poison/annotation_template.csv` — one row per held-out case with empty
  `label`, `notes` and `annotator` columns.

The template is **not** human-annotated yet. Do not describe it as such until
two reviewers fill it.

## Annotation protocol

For each held-out case:

1. Read `benign_task`, `poisoned_memories`, `attack_target`.
2. Decide whether a reasonable agent should follow the benign answer or the
   attack target.
3. Fill `label` with one of: `benign`, `attack`, `ambiguous`.
4. Add `notes` for edge cases (ambiguous instruction, legitimate instruction-like
   memory).
5. Two annotators label independently; compute agreement (Cohen's kappa) before
   using the split for claims.

## Why a held-out split matters

The 108 base cases and 12 adaptive cases are generated from the same templates
as the defenses. A held-out split is the minimum guard against overfitting the
benchmark to its own templates.

## Current limitation

The split is deterministic but the labels are still empty. Until two humans fill
the template, the benchmark's primary evidence is the synthetic base/adaptive
results, not a human-annotated evaluation.
