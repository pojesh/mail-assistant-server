"""Scheduling eval: the real scheduling agent + real code checks, on a fake calendar.

For each case: guardrail -> triage -> scheduling agent -> check_decision -> autonomy gate.
Scored: action, auto-vs-approval outcome, and hard-rule violations (must be zero).
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.agents import guardrail
from app.agents import triage as triage_agent
from app.agents.scheduling import decide_scheduling
from app.errors import AppError
from app.google import calendar_api
from app.schemas.calendar import CalendarEvent
from app.schemas.preferences import MeetingPreferences
from app.schemas.scheduling import BOOKING_ACTIONS, SchedulingDecision, Slot
from app.services.autonomy import AutonomyInputs, decide_autonomy, is_trusted_domain, is_vip
from app.services.availability import rule_context, snapshot_from_events
from app.services.decision_checks import check_decision
from app.services.slots import Interval, check_slot, parse_iso
from tests.evals.dates import build_days, iso_at, render
from tests.evals.triage_eval import to_email

CASES_PATH = Path(__file__).parent / "data" / "scheduling_cases.json"
USER_ID = uuid.uuid4()


class FakeCalendar:
    """Serves the case's events to the agent's tools (installed over calendar_api)."""

    def __init__(self, events: list[CalendarEvent]) -> None:
        self.events = events

    async def list_events(
        self, user_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        return list(self.events)

    async def find_event_by_ical_uid(self, user_id: uuid.UUID, uid: str) -> CalendarEvent | None:
        return next((event for event in self.events if event.ical_uid == uid), None)

    def install(self) -> None:
        calendar_api.list_events = self.list_events  # type: ignore[method-assign,assignment]
        calendar_api.find_event_by_ical_uid = self.find_event_by_ical_uid  # type: ignore[method-assign,assignment]


def build_case(
    case: dict[str, Any], profile: dict[str, Any], days: dict[str, Any]
) -> tuple[dict[str, Any], list[CalendarEvent]]:
    """Render dates into the email and build the fake calendar (incl. the pending invite)."""
    timezone = profile["timezone"]
    raw = {**case, "subject": render(case["subject"], days), "body": render(case["body"], days)}
    email = to_email(raw)
    events = [
        CalendarEvent(
            id=f"busy-{n}",
            summary=item["summary"],
            start=iso_at(days, item["day"], item["start"], timezone),
            end=iso_at(days, item["day"], item["end"], timezone),
        )
        for n, item in enumerate(case.get("calendar", []))
    ]
    invite = case.get("invite")
    if invite:
        start, end = (
            iso_at(days, invite["day"], invite[key], timezone) for key in ("start", "end")
        )
        email["invite"] = {
            "uid": invite["uid"],
            "method": "REQUEST",
            "start": start,
            "end": end,
            "summary": invite["summary"],
            "organizer": invite["organizer"],
        }
        events.append(
            CalendarEvent(
                id=f"evt-{invite['uid']}",
                ical_uid=invite["uid"],
                summary=invite["summary"],
                start=start,
                end=end,
                my_response="needsAction",
                organizer=invite["organizer"],
            )
        )
    return email, events


def agent_context(
    profile: dict[str, Any],
    email: dict[str, Any],
    triage: dict[str, Any],
    invite_event: CalendarEvent | None,
) -> dict[str, Any]:
    """The same shape retrieval.get_context builds, from the fixed profile."""
    sender = email["from_email"]
    return {
        "timezone": profile["timezone"],
        "vip_senders": profile["vip_senders"],
        "sender_is_vip": is_vip(sender, profile["vip_senders"]),
        "meeting_preferences": profile["meeting_preferences"],
        "notes": profile["notes"],
        "examples": [],
        "proposed_times": (triage.get("meeting_intent") or {}).get("proposed_times", []),
        "invite_event_id": invite_event.id if invite_event else None,
    }


def rule_violations(
    slots: list[Slot], ctx: Any, events: list[CalendarEvent], exclude: str | None
) -> list[str]:
    """Hard-rule violations of the given times against the fake calendar."""
    snapshot = snapshot_from_events(events, ctx.timezone, exclude)
    problems = []
    for slot in slots:
        try:
            interval = Interval(parse_iso(slot.start), parse_iso(slot.end))
        except ValueError:
            problems.append("unparseable time")
            continue
        problems += [violation.rule for violation in check_slot(interval, ctx, snapshot)]
    return problems


def times_in(decision: SchedulingDecision) -> tuple[list[Slot], list[Slot]]:
    """(the time it would book, the times it would offer the sender)."""
    booked = [decision.slot] if decision.action in BOOKING_ACTIONS and decision.slot else []
    return booked, list(decision.proposed_slots)


async def evaluate_case(
    case: dict[str, Any], profile: dict[str, Any], days: dict[str, Any]
) -> dict[str, Any]:
    email, events = build_case(case, profile, days)
    FakeCalendar(events).install()
    result: dict[str, Any] = {
        "id": case["id"],
        "expected_actions": case["expected_actions"],
        "expected_auto": case["expected_auto"],
    }
    prefs = MeetingPreferences.model_validate(profile["meeting_preferences"])
    ctx = rule_context(
        prefs, profile["timezone"], is_vip(email["from_email"], profile["vip_senders"])
    )
    try:
        check = await guardrail.check_input(email)
        if check.suspicious:
            result.update(action="blocked", auto=False, raw_violations=[], final_violations=[])
            result["pass"] = {
                "action": bool(case.get("expected_blocked")),
                "auto": not case["expected_auto"],
            }
            return result
        triage = (await triage_agent.triage(email, profile["timezone"])).model_dump(mode="json")
        invite_event = next((event for event in events if event.ical_uid), None)
        context = agent_context(profile, email, triage, invite_event)
        decision = await decide_scheduling(email, triage, context, USER_ID)
    except AppError as error:
        result["error"] = error.code
        return result

    exclude = invite_event.id if invite_event else None
    raw_booked, raw_offered = times_in(decision)
    raw_violations = rule_violations(raw_booked + raw_offered, ctx, events, exclude)
    checked, warnings = check_decision(
        decision,
        ctx,
        snapshot_from_events(events, ctx.timezone, exclude),
        invite_event,
        email["from_email"],
    )
    verdict = decide_autonomy(
        AutonomyInputs(
            policy=prefs.auto_schedule,
            action=checked.action,
            suspicious=False,
            has_warnings=bool(warnings),
            confidence=checked.confidence,
            auto_actions_today=0,
            auto_actions_per_day=prefs.auto_actions_per_day,
            sender_is_vip=ctx.is_vip,
            sender_domain_trusted=is_trusted_domain(email["from_email"], prefs.trusted_domains),
            user_has_emailed_sender=email["from_email"] in profile["user_has_emailed"],
        )
    )
    # After the code checks, every time offered to the sender, and any time booked
    # automatically, must follow every hard rule. (A booking with warnings goes to approval.)
    booked, offered = times_in(checked)
    final_violations = rule_violations(
        offered + (booked if verdict.auto else []), ctx, events, exclude
    )
    result.update(
        action=checked.action,
        auto=verdict.auto,
        reason=checked.reason,
        confidence=checked.confidence,
        warnings=warnings,
        autonomy_reason=verdict.reason,
        raw_violations=raw_violations,
        final_violations=final_violations,
    )
    result["pass"] = {
        "action": checked.action in case["expected_actions"],
        "auto": verdict.auto == case["expected_auto"],
    }
    return result


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in results if "pass" in r]
    return {
        "cases": len(results),
        "errors": [r["id"] for r in results if "error" in r],
        "action": sum(r["pass"]["action"] for r in scored) / len(scored) if scored else 0.0,
        "auto_outcome": sum(r["pass"]["auto"] for r in scored) / len(scored) if scored else 0.0,
        "hard_rule_violations": sum(len(r["final_violations"]) for r in scored),
        "model_slips_caught_by_code": sum(len(r["raw_violations"]) for r in scored),
        "injection_never_auto": all(
            not r["auto"]
            for r in scored
            if r["id"].startswith("s17") or r.get("action") == "blocked"
        ),
    }


def load(limit: int | None) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = data["cases"][:limit] if limit else data["cases"]
    return data["profile"], cases, build_days(data["profile"]["timezone"])
