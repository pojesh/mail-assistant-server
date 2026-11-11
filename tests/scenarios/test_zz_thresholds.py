"""Aggregate quality gates. Runs last (file name) and checks every suite that ran in this
session against its targets (tests/scenarios/recorder.py THRESHOLDS)."""

import pytest

from tests.scenarios.recorder import THRESHOLDS, Recorder, check_thresholds, summarise

GATES = [(suite, label) for suite, checks in THRESHOLDS.items() for label, *_ in checks]


@pytest.mark.parametrize(("suite", "label"), GATES, ids=[f"{s}: {label}" for s, label in GATES])
def test_threshold(suite: str, label: str, recorder: Recorder) -> None:
    cases = recorder.cases.get(suite)
    if not cases:
        pytest.skip(f"{suite} did not run in this session")
    check = next(c for c in check_thresholds(suite, summarise(suite, cases)) if c["label"] == label)
    if check["passed"] is None:
        pytest.skip(f"no data for {label}")
    assert check["passed"], (
        f"{label}: {check['value']} (target {check['comparison']} {check['target']})"
    )
