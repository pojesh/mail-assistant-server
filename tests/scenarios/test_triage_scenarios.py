"""Triage scenarios: categories, priority, sentiment, needs-reply, meeting detection and
prompt-injection screening, on 45 synthetic emails with hand-written expected answers.

Each email is one test. A normal email passes when category, needs-reply and meeting detection
match and the priority falls in its expected range (sentiment is scored in aggregate only,
since it is the most subjective label). An injection email passes when it is flagged.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.agents import guardrail
from app.agents import triage as triage_agent
from app.errors import AppError
from tests.evals import triage_eval
from tests.scenarios.conftest import measure
from tests.scenarios.recorder import Recorder

TIMEZONE = "Asia/Kolkata"
CASES = triage_eval.load_cases(None)
PRIORITY_RANGES: dict[str, list[int]] = json.loads(
    (Path(__file__).parent / "data" / "priorities.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
async def test_triage(case: dict[str, Any], recorder: Recorder) -> None:
    email = triage_eval.to_email(case)
    expected = case["expected"]
    row: dict[str, Any] = {"id": case["id"], "from": case["from"], "subject": case["subject"]}
    try:
        with measure() as meter:
            check = await guardrail.check_input(email)
        recorder.time("triage", "Input guardrail", meter)

        if "suspicious" in expected:  # prompt-injection email: must be flagged, nothing else
            row.update(
                kind="injection",
                expected=expected,
                got={"suspicious": check.suspicious, "reasons": check.reasons},
                passed=check.suspicious,
            )
            recorder.add("triage", row)
            assert check.suspicious, f"{case['id']}: injection email was not flagged"
            return

        with measure() as meter:
            found = triage_agent.boost_priority(
                await triage_agent.triage(email, TIMEZONE), sender_is_vip=False
            )
        recorder.time("triage", "Triage", meter)
    except AppError as error:
        row.update(kind="injection" if "suspicious" in expected else "normal", error=error.code)
        recorder.add("triage", row)
        pytest.fail(f"{case['id']}: model call failed ({error.code})")

    wants_meeting = (
        bool(found.meeting_intent and found.meeting_intent.wants_meeting)
        or found.category == "meeting_request"
    )
    low, high = PRIORITY_RANGES[case["id"]]
    checks = {
        "category": found.category == expected["category"],
        "priority": low <= found.priority <= high,
        "needs_reply": found.needs_reply == expected["needs_reply"],
        "meeting": wants_meeting == expected["wants_meeting"],
        "sentiment": found.sentiment == expected["sentiment"],
        "no_false_alarm": not check.suspicious,
    }
    required = ("category", "priority", "needs_reply", "meeting", "no_false_alarm")
    row.update(
        kind="normal",
        expected=expected,
        priority_range=[low, high],
        got={
            "category": found.category,
            "priority": found.priority,
            "sentiment": found.sentiment,
            "needs_reply": found.needs_reply,
            "wants_meeting": wants_meeting,
            "summary": found.summary,
            "suspicious": check.suspicious,
        },
        checks=checks,
        passed=all(checks[key] for key in required),
    )
    recorder.add("triage", row)
    misses = [key for key in required if not checks[key]]
    assert not misses, f"{case['id']}: {misses} wrong; got {row['got']}"
