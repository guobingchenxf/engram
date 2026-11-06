"""Held-out split and human annotation tooling.

The split is deterministic; the labels are *not* human-provided until a reviewer
fills the template. Do not report it as human-annotated before that happens.
"""

from __future__ import annotations

import csv
import random
from collections.abc import Sequence
from pathlib import Path

from .cases import PoisonCase, build_cases, write_jsonl


def select_heldout(
    cases: Sequence[PoisonCase] | None = None,
    *,
    ratio: float = 0.2,
    seed: int = 7,
) -> tuple[PoisonCase, ...]:
    selected = tuple(cases or build_cases())
    count = max(1, round(len(selected) * ratio))
    indices = sorted(random.Random(seed).sample(range(len(selected)), count))
    return tuple(selected[index] for index in indices)


def write_heldout(
    path: str | Path,
    cases: Sequence[PoisonCase] | None = None,
) -> Path:
    return write_jsonl(path, select_heldout(cases))


def write_annotation_template(
    path: str | Path,
    cases: Sequence[PoisonCase] | None = None,
) -> Path:
    selected = cases or select_heldout()
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "case_id",
                "channel",
                "strategy",
                "goal",
                "benign_task",
                "attack_target",
                "label",
                "notes",
                "annotator",
            ]
        )
        for case in selected:
            writer.writerow(
                [
                    case.id,
                    case.channel,
                    case.strategy,
                    case.goal,
                    case.benign_task,
                    case.attack_target,
                    "",
                    "",
                    "",
                ]
            )
    return output
