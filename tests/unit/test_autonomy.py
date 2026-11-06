"""Auto-scheduling policy: policy level x sender trust x confidence x cap x suspicious x action."""

from dataclasses import replace

import pytest

from app.services.autonomy import (
    AutonomyInputs,
    decide_autonomy,
    email_domain,
    is_trusted_domain,
    is_vip,
)

ALLOWED = AutonomyInputs(
    policy="trusted",
    action="accept_invite",
    suspicious=False,
    has_warnings=False,
    confidence=0.9,
    auto_actions_today=0,
    auto_actions_per_day=5,
    sender_is_vip=True,
    sender_domain_trusted=False,
    user_has_emailed_sender=False,
)
UNTRUSTED = replace(ALLOWED, sender_is_vip=False)


def test_trusted_sender_is_auto() -> None:
    assert decide_autonomy(ALLOWED).auto


@pytest.mark.parametrize("action", ["accept_invite", "tentative_invite", "create_event"])
def test_auto_actions(action: str) -> None:
    assert decide_autonomy(replace(ALLOWED, action=action)).auto


@pytest.mark.parametrize(
    "action", ["decline_invite", "propose_new_time", "propose_times", "ignore"]
)
def test_declines_and_proposals_always_need_approval(action: str) -> None:
    verdict = decide_autonomy(replace(ALLOWED, action=action, policy="all"))
    assert not verdict.auto
    assert "approval" in verdict.reason


@pytest.mark.parametrize(
    ("change", "reason_word"),
    [
        ({"policy": "off"}, "off"),
        ({"suspicious": True}, "suspicious"),
        ({"has_warnings": True}, "rules"),
        ({"confidence": 0.69}, "confident"),
        ({"auto_actions_today": 5}, "limit"),
    ],
)
def test_each_rule_blocks(change: dict[str, object], reason_word: str) -> None:
    verdict = decide_autonomy(replace(ALLOWED, **change))  # type: ignore[arg-type]
    assert not verdict.auto
    assert reason_word in verdict.reason


def test_confidence_threshold_is_inclusive() -> None:
    assert decide_autonomy(replace(ALLOWED, confidence=0.7)).auto


@pytest.mark.parametrize(
    "trust",
    [{"sender_is_vip": True}, {"sender_domain_trusted": True}, {"user_has_emailed_sender": True}],
)
def test_any_trust_signal_is_enough(trust: dict[str, bool]) -> None:
    assert decide_autonomy(replace(UNTRUSTED, **trust)).auto  # type: ignore[arg-type]


def test_untrusted_sender_needs_approval_on_trusted_policy() -> None:
    verdict = decide_autonomy(UNTRUSTED)
    assert not verdict.auto and "trusted" in verdict.reason


def test_all_policy_allows_untrusted_sender() -> None:
    assert decide_autonomy(replace(UNTRUSTED, policy="all")).auto


def test_all_policy_still_blocks_suspicious_and_over_cap() -> None:
    assert not decide_autonomy(replace(UNTRUSTED, policy="all", suspicious=True)).auto
    assert not decide_autonomy(replace(UNTRUSTED, policy="all", auto_actions_today=9)).auto


def test_vip_matching() -> None:
    vips = ["boss@corp.com", "@client.com"]
    assert is_vip("Boss@Corp.com", vips)
    assert is_vip("anyone@client.com", vips)
    assert not is_vip("someone@corp.com", vips)
    assert not is_vip("x@notclient.com", vips)


def test_domains() -> None:
    assert email_domain("Ravi@Client.COM") == "client.com"
    assert is_trusted_domain("a@acme.io", ["@ACME.io"])
    assert not is_trusted_domain("a@acme.io.evil.com", ["acme.io"])
