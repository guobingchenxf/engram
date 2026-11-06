"""Submission format for external MemPoison results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

DATASET_VERSION = "mempoison-0.1"


class SubmissionMetrics(BaseModel):
    asr: float = Field(ge=0.0, le=1.0)
    ur: float = Field(ge=0.0, le=1.0)
    fpr: float = Field(ge=0.0, le=1.0)
    detection_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    adaptive_asr: float = Field(default=0.0, ge=0.0, le=1.0)


class Submission(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    defense: str = Field(min_length=1, max_length=120)
    dataset_version: str = DATASET_VERSION
    split: str = Field(default="full")
    metrics: SubmissionMetrics
    case_results: list[dict[str, Any]] = Field(default_factory=list)
    notes: str = ""

    @field_validator("dataset_version")
    @classmethod
    def _version(cls, value: str) -> str:
        if value != DATASET_VERSION:
            raise ValueError(
                f"unsupported dataset_version {value!r}; expected {DATASET_VERSION!r}"
            )
        return value


def load_submission(path: str | Path) -> Submission:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Submission.model_validate(data)


def write_submission(path: str | Path, submission: Submission) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        submission.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return output
