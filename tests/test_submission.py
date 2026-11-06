from __future__ import annotations

import csv

import pytest
from pydantic import ValidationError

from engram.poison.annotation import select_heldout, write_annotation_template
from engram.poison.cases import build_cases
from engram.poison.submission import (
    Submission,
    SubmissionMetrics,
    load_submission,
    write_submission,
)


def test_heldout_split_is_deterministic_and_unique() -> None:
    first = select_heldout(build_cases())
    second = select_heldout(build_cases())
    assert len(first) == 24
    assert [case.id for case in first] == [case.id for case in second]
    assert len({case.id for case in first}) == 24


def test_annotation_template(tmp_path) -> None:
    path = write_annotation_template(tmp_path / "annotation.csv")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0][0] == "case_id"
    assert len(rows) == 25


def test_submission_round_trip(tmp_path) -> None:
    submission = Submission(
        name="test",
        defense="provenance",
        metrics=SubmissionMetrics(asr=0.1, ur=0.9, fpr=0.0),
    )
    path = write_submission(tmp_path / "submission.json", submission)
    loaded = load_submission(path)
    assert loaded.defense == "provenance"
    assert loaded.metrics.asr == 0.1


def test_submission_rejects_wrong_version() -> None:
    with pytest.raises(ValidationError):
        Submission(
            name="test",
            defense="none",
            dataset_version="other",
            metrics=SubmissionMetrics(asr=1.0, ur=0.0, fpr=0.0),
        )
