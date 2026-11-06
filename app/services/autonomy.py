"""The auto-scheduling policy gate: may a calendar action run without asking the user?

Pure functions only. Anything that is not clearly allowed goes to the approvals queue.
"""

from dataclasses import dataclass
from typing import Literal

AUTO_ACTIONS = {"accept_invite", "tentative_invite", "create_event"}
MIN_CONFIDENCE = 0.7


@dataclass(frozen=True)
class AutonomyInputs:
    policy: Literal["off", "trusted", "all"]
    action: str
    suspicious: bool
    has_warnings: bool
    confidence: float
    auto_actions_today: int
    auto_actions_per_day: int
    sender_is_vip: bool
    sender_domain_trusted: bool
    user_has_emailed_sender: bool


@dataclass(frozen=True)
class AutonomyVerdict:
    auto: bool
    reason: str  # why it was (or was not) done automatically, shown in the activity log


def email_domain(address: str) -> str:
    """'Ravi@Client.com' -> 'client.com'"""
    return address.rsplit("@", 1)[-1].strip().lower()


def is_vip(address: str, vip_senders: list[str]) -> bool:
    """VIP entries are full addresses or whole domains written as '@client.com'."""
    address = address.strip().lower()
    for entry in vip_senders:
        entry = entry.strip().lower()
        if entry == address or (entry.startswith("@") and email_domain(address) == entry[1:]):
            return True
    return False


def is_trusted_domain(address: str, trusted_domains: list[str]) -> bool:
    return email_domain(address) in {domain.lower().lstrip("@") for domain in trusted_domains}


def _sender_is_trusted(inputs: AutonomyInputs) -> bool:
    return inputs.sender_is_vip or inputs.sender_domain_trusted or inputs.user_has_emailed_sender


def decide_autonomy(inputs: AutonomyInputs) -> AutonomyVerdict:
    """Auto-execute only if every rule passes (spec §10.2); otherwise ask the user."""
    if inputs.policy == "off":
        return AutonomyVerdict(False, "Auto-scheduling is off")
    if inputs.action not in AUTO_ACTIONS:
        return AutonomyVerdict(False, "Declines and proposals always need your approval")
    if inputs.suspicious:
        return AutonomyVerdict(False, "The email looks suspicious")
    if inputs.has_warnings:
        return AutonomyVerdict(False, "The time breaks one of your rules")
    if inputs.confidence < MIN_CONFIDENCE:
        return AutonomyVerdict(False, "The assistant was not confident enough")
    if inputs.auto_actions_today >= inputs.auto_actions_per_day:
        return AutonomyVerdict(False, "Today's limit of automatic actions was reached")
    if inputs.policy == "trusted" and not _sender_is_trusted(inputs):
        return AutonomyVerdict(False, "The sender is not one of your trusted contacts")
    return AutonomyVerdict(True, "Allowed by your auto-scheduling settings")
