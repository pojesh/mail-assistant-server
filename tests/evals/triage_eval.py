"""Triage + input-guardrail eval against golden emails (real model calls)."""

import json
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from app.agents import guardrail
from app.agents import triage as triage_agent
from app.errors import AppError

CASES_PATH = Path(__file__).parent / "data" / "triage_cases.json"


def to_email(case: dict[str, Any]) -> dict[str, Any]:
    name, address = parseaddr(case["from"])
    return {
        "from_email": address.lower(),
        "from_name": name or None,
        "to": ["me@example.com"],
        "subject": case["subject"],
        "body_text": case["body"],
        "received_at": "2026-11-06T09:00:00+05:30",
        "invite": case.get("invite"),
    }


async def evaluate_case(case: dict[str, Any], timezone: str) -> dict[str, Any]:
    """Run the guardrail and (for normal emails) triage; compare with the expected labels."""
    email = to_email(case)
    expected = case["expected"]
    result: dict[str, Any] = {"id": case["id"], "expected": expected}
    try:
        check = await guardrail.check_input(email)
        result["suspicious"] = check.suspicious
        if "suspicious" in expected:  # prompt-injection case
            result["pass"] = {"suspicious": check.suspicious}
            return result
        found = triage_agent.boost_priority(
            await triage_agent.triage(email, timezone), sender_is_vip=False
        )
    except AppError as error:
        result["error"] = error.code
        return result

    wants_meeting = (
        bool(found.meeting_intent and found.meeting_intent.wants_meeting)
        or found.category == "meeting_request"
    )
    result["got"] = {
        "category": found.category,
        "sentiment": found.sentiment,
        "needs_reply": found.needs_reply,
        "wants_meeting": wants_meeting,
        "priority": found.priority,
        "summary": found.summary,
    }
    result["pass"] = {
        key: result["got"][key] == expected[key]
        for key in ("category", "sentiment", "needs_reply", "wants_meeting")
    }
    result["false_alarm"] = check.suspicious  # a normal email flagged as an injection
    return result


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    normal = [r for r in results if "got" in r]
    injections = [r for r in results if "suspicious" in r["expected"]]

    flagged = sum(bool(r.get("suspicious")) for r in injections)

    def rate(key: str) -> float:
        return sum(r["pass"][key] for r in normal) / len(normal) if normal else 0.0

    return {
        "cases": len(results),
        "errors": [r["id"] for r in results if "error" in r],
        "category": rate("category"),
        "sentiment": rate("sentiment"),
        "needs_reply": rate("needs_reply"),
        "wants_meeting": rate("wants_meeting"),
        "injections_flagged": f"{flagged}/{len(injections)}",
        "all_injections_flagged": all(r.get("suspicious") for r in injections),
        "false_alarms": [r["id"] for r in normal if r.get("false_alarm")],
    }


def load_cases(limit: int | None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return cases[:limit] if limit else cases
