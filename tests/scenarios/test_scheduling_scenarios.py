"""Scheduling scenarios: 17 meeting emails (invites, requests, rule clashes, an injection) run
through guardrail -> triage -> scheduling agent -> code checks -> autonomy gate, on a fake
calendar. A case passes when the action is acceptable, the auto-vs-approval outcome is right
and no time that reaches the user breaks a hard rule.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from app.google import calendar_api
from tests.evals import scheduling_eval
from tests.evals.dates import render
from tests.scenarios.conftest import measure
from tests.scenarios.recorder import Recorder

PROFILE, CASES, DAYS = scheduling_eval.load(None)


@pytest.fixture(autouse=True)
def restore_calendar() -> Iterator[None]:
    """evaluate_case installs a fake calendar by assignment; put the real functions back."""
    saved = (calendar_api.list_events, calendar_api.find_event_by_ical_uid)
    yield
    calendar_api.list_events, calendar_api.find_event_by_ical_uid = saved


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
async def test_scheduling(case: dict[str, Any], recorder: Recorder) -> None:
    with measure() as meter:
        result = await scheduling_eval.evaluate_case(case, PROFILE, DAYS)
    row: dict[str, Any] = {
        "id": case["id"],
        "from": case["from"],
        "subject": render(case["subject"], DAYS),
        "expected_actions": case["expected_actions"],
        "expected_auto": case["expected_auto"],
    }
    if "error" in result:
        row["error"] = result["error"]
        recorder.add("scheduling", row)
        pytest.fail(f"{case['id']}: model call failed ({result['error']})")

    recorder.time("scheduling", "Guardrail + triage + scheduling agent", meter)
    checks = {**result["pass"], "no_violations": not result["final_violations"]}
    row.update(
        action=result["action"],
        auto=result["auto"],
        reason=result.get("reason"),
        confidence=result.get("confidence"),
        warnings=result.get("warnings", []),
        autonomy_reason=result.get("autonomy_reason"),
        raw_violations=result["raw_violations"],
        final_violations=result["final_violations"],
        checks=checks,
        passed=all(checks.values()),
    )
    recorder.add("scheduling", row)
    assert row["passed"], (
        f"{case['id']}: action={result['action']} (expected {case['expected_actions']}), "
        f"auto={result['auto']} (expected {case['expected_auto']}), "
        f"violations={result['final_violations']}"
    )
