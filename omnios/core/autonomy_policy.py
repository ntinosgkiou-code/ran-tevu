"""
autonomy_policy.py — the doctor governs by POLICY, not by clicking each action.
==============================================================================
This is what turns "the AI proposes, you approve every item" into "the AI RUNS the back office
on rules you set once". The novelty is not autonomy alone (agents that act exist) nor governance
alone (copilots that suggest exist) — it is autonomy WITH a provable, revocable governance layer:

  • The doctor enables autonomy and picks, PER ACTION TYPE, what may execute on its own, with a
    euro ceiling. Everything is OFF by default (opt-in — EU AI Act Art.14 human oversight is the
    setting of the policy; the human stays in control of the rules, not the keystrokes).
  • Consent (GDPR Art.9) is ALWAYS required and is checked upstream at propose-time, so an
    autonomously-executed action can never reach a patient who withheld consent.
  • Every autonomous execution is written to the tamper-evident audit chain WITH the policy that
    authorized it (what / why / evidence / who-set-the-rule) — a regulator-ready flight recorder.
  • A single master switch (`enabled=False`) is an instant kill switch; every action stays
    reversible and capped (per-euro and per-sweep) so a runaway can't happen.

Deterministic, stdlib + the clinic store. Tests: python core/test_autonomy.py
"""
from __future__ import annotations

from typing import Any

# Sensible euro ceilings per type (a proposal above the ceiling always falls back to human review).
# 0 / falsy ceiling = "no auto for a priced action" is NOT the meaning — see authorizes(): a €0
# action (e.g. a reminder) is always within any ceiling; a ceiling only caps priced actions.
_DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "no_show_recovery": {"auto": False, "max_eur": 500},
    "reminder":         {"auto": False, "max_eur": 0},
    "recall":           {"auto": False, "max_eur": 300},
    "dunning":          {"auto": False, "max_eur": 300},
    "waitlist_fill":    {"auto": False, "max_eur": 300},
    "billing":          {"auto": False, "max_eur": 1000},
}

_LABELS = {
    "no_show_recovery": "Ανάκτηση no-show",
    "reminder": "Υπενθυμίσεις ραντεβού",
    "recall": "Recall / ανανέωση πακέτου",
    "dunning": "Υπενθυμίσεις οφειλών",
    "waitlist_fill": "Γέμισμα κενής θέσης",
    "billing": "Έκδοση τιμολογίων",
}


def default_policy() -> dict[str, Any]:
    return {
        "enabled": False,                      # master switch — opt-in, OFF until the doctor turns it on
        "rules": {k: dict(v) for k, v in _DEFAULT_RULES.items()},
        "max_actions_per_sweep": 25,           # runaway backstop
        "set_by": "", "set_at": "",
    }


class AutonomyPolicy:
    """Per-clinic autonomy policy, persisted in the clinic's own store (survives restart)."""

    def __init__(self, store) -> None:
        self.cs = store

    def _ns(self) -> str:
        return self.cs._ns("autonomy")

    def get(self) -> dict[str, Any]:
        pol = self.cs.kv.get(self._ns(), "policy")
        if not pol:
            return default_policy()
        # merge onto defaults so a newly-added action type is never missing a rule
        base = default_policy()
        base.update({k: v for k, v in pol.items() if k != "rules"})
        base["rules"].update(pol.get("rules", {}))
        return base

    def set(self, *, enabled: bool | None = None, rules: dict | None = None,
            max_actions_per_sweep: int | None = None, set_by: str = "") -> dict[str, Any]:
        pol = self.get()
        if enabled is not None:
            pol["enabled"] = bool(enabled)
        if isinstance(rules, dict):
            for t, r in rules.items():
                if t not in pol["rules"] or not isinstance(r, dict):
                    continue                       # ignore unknown types / malformed entries
                if "auto" in r:
                    pol["rules"][t]["auto"] = bool(r["auto"])
                if "max_eur" in r:
                    try:                           # reject garbage (e.g. "abc") instead of 500-ing
                        pol["rules"][t]["max_eur"] = max(0.0, float(r["max_eur"]))
                    except (TypeError, ValueError):
                        pass                       # keep the previous ceiling on bad input
        if max_actions_per_sweep is not None:
            pol["max_actions_per_sweep"] = max(1, int(max_actions_per_sweep))
        pol["set_by"] = set_by or pol.get("set_by", "")
        pol["set_at"] = _stamp()
        self.cs.kv.put(self._ns(), "policy", pol)
        self.cs._audit("autonomy.policy_set", {"enabled": pol["enabled"], "by": pol["set_by"],
                        "auto_types": [t for t, r in pol["rules"].items() if r["auto"]]})
        return pol

    def authorizes(self, proposal: dict) -> tuple[bool, str]:
        """May THIS proposal execute autonomously right now? (consent is re-checked at execution)"""
        pol = self.get()
        if not pol.get("enabled"):
            return False, "autonomy off (master switch)"
        rule = pol["rules"].get(proposal.get("type", ""))
        if not rule or not rule.get("auto"):
            return False, f"no auto-rule for '{proposal.get('type')}'"
        est = float(proposal.get("est_eur") or 0)
        # SAFETY: a PRICED action auto-executes only within a POSITIVE ceiling. A missing/zero
        # ceiling means "do NOT auto-send anything with money on it" (fail safe → human review),
        # NOT "uncapped". A €0 action (e.g. a reminder) carries no money and always passes.
        if est > 0:
            ceiling = float(rule.get("max_eur") or 0)
            if ceiling <= 0:
                return False, "priced action needs a positive € ceiling → human review"
            if est > ceiling:
                return False, f"€{est:g} over policy ceiling €{ceiling:g} → human review"
        return True, "authorized by policy"

    def summary(self) -> dict[str, Any]:
        pol = self.get()
        auto = [t for t, r in pol["rules"].items() if r["auto"]]
        return {"enabled": pol["enabled"], "auto_types": auto,
                "auto_labels": [_LABELS.get(t, t) for t in auto],
                "rules": pol["rules"], "max_actions_per_sweep": pol["max_actions_per_sweep"],
                "set_by": pol.get("set_by", ""), "set_at": pol.get("set_at", ""),
                "labels": _LABELS}


def _stamp() -> str:
    # wall-clock stamp for the audit/UI; kept local so this module has no time import at top-level use sites
    import time as _t
    return _t.strftime("%d/%m/%Y %H:%M")
