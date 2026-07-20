"""
policy.py — Declarative guardrail / policy engine.
==================================================
Deterministic, explainable, and unit-testable. Every booking intent is checked
against a small set of declarative rules BEFORE it reaches the booking actor.
Each rule returns a human-readable reason, so a denial is never a black box.

Rules operate on any object exposing `.timestamp` (ISO 'YYYY-MM-DDTHH:MM'),
`.constraints` (dict) and `.action` — no import of the app layer (no cycles).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

BUSINESS_HOURS = (8, 21)   # [open, close)
MAX_PARTY_SIZE = 8


@dataclass
class PolicyVerdict:
    allowed: bool
    reasons: List[str] = field(default_factory=list)
    adjustments: Dict[str, Any] = field(default_factory=dict)


def _hour_of(ts: str):
    try:
        return int(ts[11:13])
    except Exception:
        return None


def evaluate(frame: Any) -> PolicyVerdict:
    reasons: List[str] = []
    adjustments: Dict[str, Any] = {}
    allowed = True

    hour = _hour_of(getattr(frame, "timestamp", "") or "")
    if hour is not None and not (BUSINESS_HOURS[0] <= hour < BUSINESS_HOURS[1]):
        allowed = False
        reasons.append(f"Εκτός ωραρίου λειτουργίας ({hour:02d}:xx · ωράριο "
                       f"{BUSINESS_HOURS[0]:02d}:00–{BUSINESS_HOURS[1]:02d}:00)")

    constraints = getattr(frame, "constraints", {}) or {}
    if constraints.get("priority") == "urgent":
        adjustments["priority"] = "urgent"
        reasons.append("Επείγον περιστατικό — προτεραιότητα στην ουρά")

    party = constraints.get("party_size")
    if isinstance(party, int) and party > MAX_PARTY_SIZE:
        allowed = False
        reasons.append(f"party_size {party} > μέγιστο {MAX_PARTY_SIZE}")

    if not reasons:
        reasons.append("Εντός όλων των πολιτικών")
    return PolicyVerdict(allowed=allowed, reasons=reasons, adjustments=adjustments)
