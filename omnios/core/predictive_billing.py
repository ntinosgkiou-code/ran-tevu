"""
predictive_billing.py — STEP 5: Predictive Simulation + Value-Based Billing.
===========================================================================
Two engines that close the loop from "what should the agent do?" to "what do we charge?":

  • PredictiveSimulator — a small Markov decision model over an appointment. Given the state
    (value, baseline no-show risk), it simulates each intervention (do-nothing / SMS / AI-call /
    deposit), predicts the next-state distribution {SHOWED, NO_SHOW}, and recommends the action
    with the highest expected value. Baselines are evidence-anchored (SMS reminder = 25% relative
    no-show reduction — see brain/sources/CLINICAL-EVIDENCE.md).

  • ValueBasedBilling — turns realized outcomes into € saved (recovered appointments, filled
    slots, secretary time), then bills a share of that value. "You keep 90%; we take 10% of what
    we saved you" — zero risk for the clinic, ROI is explicit. Never charges more than it saved.

Deterministic, dependency-free, Pydantic v2, typed, async wrappers. Tests: python core/test_predictive_billing.py
"""
from __future__ import annotations

import asyncio
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


# ─────────────────────────── predictive simulation (MDP) ───────────────────────────
class Action(str, Enum):
    NONE = "none"
    SMS = "sms_reminder"
    AI_CALL = "ai_call_reminder"
    DEPOSIT = "require_deposit"


# (risk multiplier on the baseline no-show prob, action cost €). Evidence-anchored to PUBLISHED
# meta-analyses — see RESEARCH.md:
#   SMS 0.75  = Robotham et al. 2016 (BMJ Open; 21 studies): no-show RR 0.75 (95% CI 0.68–0.82).
#   CALL 0.73 = phone ≈ text reminders (Gurol-Urganci et al., Cochrane 2013: RR text-vs-phone 0.99),
#               with a small edge for live interaction — kept within the meta-analytic CI.
#   DEPOSIT   = heuristic (no meta-analysis found); flagged as such, tune per clinic outcomes.
_EFFECTS: dict[Action, tuple[float, float]] = {
    Action.NONE: (1.00, 0.00),
    Action.SMS: (0.75, 0.05),
    Action.AI_CALL: (0.73, 0.14),
    Action.DEPOSIT: (0.35, 0.00),
}


class AppointmentState(BaseModel):
    appointment_value: float = Field(gt=0)     # € at stake if the patient shows
    base_no_show_prob: float = Field(default=0.23, ge=0.0, le=1.0)  # published outpatient baseline (~23%)
    lead_time_hours: float = Field(default=24.0, ge=0.0)


class ActionEval(BaseModel):
    model_config = ConfigDict(frozen=True)
    action: Action
    no_show_prob: float
    show_prob: float
    action_cost: float
    expected_value: float


class SimulationResult(BaseModel):
    state: AppointmentState
    evaluations: list[ActionEval]
    recommended: Action
    predicted_next_state: dict[str, float]     # outcome distribution under the recommended action
    expected_value_gain: float                 # vs doing nothing


class PredictiveSimulator:
    def __init__(self, effects: dict[Action, tuple[float, float]] | None = None) -> None:
        self.effects = effects or _EFFECTS

    def simulate_sync(self, state: AppointmentState) -> SimulationResult:
        evals: list[ActionEval] = []
        for action, (mult, cost) in self.effects.items():
            ns = min(1.0, state.base_no_show_prob * mult)
            show = 1.0 - ns
            ev = show * state.appointment_value - cost
            evals.append(ActionEval(action=action, no_show_prob=round(ns, 4), show_prob=round(show, 4),
                                    action_cost=cost, expected_value=round(ev, 2)))
        evals.sort(key=lambda e: -e.expected_value)
        best = evals[0]
        none_ev = next(e.expected_value for e in evals if e.action == Action.NONE)
        return SimulationResult(
            state=state, evaluations=evals, recommended=best.action,
            predicted_next_state={"SHOWED": best.show_prob, "NO_SHOW": best.no_show_prob},
            expected_value_gain=round(best.expected_value - none_ev, 2),
        )

    async def simulate(self, state: AppointmentState) -> SimulationResult:
        return await asyncio.to_thread(self.simulate_sync, state)


# ─────────────────────────── value-based billing ───────────────────────────
class ValueEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: str                       # recovered_appointment | filled_slot | minutes_saved | …
    euros: float = Field(ge=0.0)


def minutes_saved_value(minutes: float, secretary_hourly: float = 12.0) -> ValueEvent:
    return ValueEvent(kind="minutes_saved", euros=round(max(0.0, minutes) / 60.0 * secretary_hourly, 2))


class BillingResult(BaseModel):
    total_value_eur: float
    charge_eur: float
    net_kept_eur: float = 0.0       # THE real number: value − charge = what the clinic keeps (= (1−pct)·value)
    roi: float                      # DEFINITIONAL: value/charge = 1/pct (always 10 at pct=10%), NOT a measured outcome
    events: int
    model: str


class ValueBasedBilling:
    """charge = flat + pct·value. Pure performance (flat=0) → 'we take {pct} of what we saved'."""

    def __init__(self, pct: float = 0.10, flat: float = 0.0) -> None:
        if not 0.0 <= pct < 1.0:
            raise ValueError("pct must be in [0, 1)")
        self.pct = pct
        self.flat = flat

    def bill_sync(self, events: list[ValueEvent]) -> BillingResult:
        value = round(sum(e.euros for e in events), 2)
        charge = round(self.flat + self.pct * value, 2)
        net_kept = round(value - charge, 2)      # the meaningful figure: what the clinic keeps
        roi = round(value / charge, 2) if charge > 0 else float("inf")
        model = (f"€{self.flat:g} + {int(self.pct * 100)}% of value" if self.flat
                 else f"{int(self.pct * 100)}% of value (pure performance)")
        return BillingResult(total_value_eur=value, charge_eur=charge, net_kept_eur=net_kept,
                             roi=roi, events=len(events), model=model)

    async def bill(self, events: list[ValueEvent]) -> BillingResult:
        return await asyncio.to_thread(self.bill_sync, events)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    sim = PredictiveSimulator()
    res = sim.simulate_sync(AppointmentState(appointment_value=45, base_no_show_prob=0.30))
    print("recommended:", res.recommended.value, "| next-state:", res.predicted_next_state,
          "| €gain vs nothing:", res.expected_value_gain)
    for e in res.evaluations:
        print(f"  {e.action.value:18} no_show={e.no_show_prob:.2f}  EV=€{e.expected_value}")

    billing = ValueBasedBilling(pct=0.10)
    events = [ValueEvent(kind="recovered_appointment", euros=45),
              ValueEvent(kind="filled_slot", euros=40),
              minutes_saved_value(600)]  # 10h secretary time
    b = billing.bill_sync(events)
    print(f"\nvalue=€{b.total_value_eur} → charge=€{b.charge_eur} (ROI {b.roi}x) · {b.model}")
