"""
test_predictive_billing.py — Step 5 tests (predictive simulation + value-based billing).
Run: python core/test_predictive_billing.py
"""
import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from predictive_billing import (Action, AppointmentState, PredictiveSimulator,
                                 ValueBasedBilling, ValueEvent, minutes_saved_value)


def test_intervention_beats_doing_nothing():
    sim = PredictiveSimulator()
    r = sim.simulate_sync(AppointmentState(appointment_value=45, base_no_show_prob=0.30))
    assert r.recommended != Action.NONE, "for a valuable slot with real risk, acting must beat doing nothing"
    assert r.expected_value_gain > 0, "recommended action must have positive expected-value gain"
    none = next(e for e in r.evaluations if e.action == Action.NONE)
    best = next(e for e in r.evaluations if e.action == r.recommended)
    assert best.expected_value > none.expected_value
    print(f"✅ MDP: recommended '{r.recommended.value}', +€{r.expected_value_gain} vs nothing")


def test_no_show_probability_drops_with_reminder():
    sim = PredictiveSimulator()
    r = sim.simulate_sync(AppointmentState(appointment_value=50, base_no_show_prob=0.30))
    none = next(e for e in r.evaluations if e.action == Action.NONE)
    sms = next(e for e in r.evaluations if e.action == Action.SMS)
    assert none.no_show_prob == 0.30 and sms.no_show_prob < none.no_show_prob
    assert abs(sms.no_show_prob - 0.30 * 0.75) < 1e-6, "SMS applies the META-ANALYTIC multiplier (Robotham 2016: RR 0.75)"
    print(f"✅ prediction: no-show 0.30 → {sms.no_show_prob:.3f} with SMS reminder (evidence-anchored)")


def test_next_state_distribution_normalized():
    sim = PredictiveSimulator()
    r = sim.simulate_sync(AppointmentState(appointment_value=45))
    d = r.predicted_next_state
    assert abs(d["SHOWED"] + d["NO_SHOW"] - 1.0) < 1e-9, "predicted next-state must be a proper distribution"
    print("✅ next-state: {SHOWED, NO_SHOW} is a normalized probability distribution")


def test_value_based_billing_and_roi():
    billing = ValueBasedBilling(pct=0.10)
    events = [ValueEvent(kind="recovered_appointment", euros=45),
              ValueEvent(kind="filled_slot", euros=40),
              minutes_saved_value(600, secretary_hourly=12.0)]   # 10h → €120
    b = billing.bill_sync(events)
    assert abs(b.total_value_eur - (45 + 40 + 120)) < 1e-6, b.total_value_eur
    assert abs(b.charge_eur - 0.10 * b.total_value_eur) < 1e-6, "charge must be 10% of value"
    assert b.charge_eur < b.total_value_eur and b.roi == 10.0, "clinic must always net positive (ROI 10x)"
    print(f"✅ billing: value €{b.total_value_eur} → charge €{b.charge_eur} (ROI {b.roi}x, never > value)")


def test_billing_edge_zero_value():
    b = ValueBasedBilling(pct=0.10).bill_sync([])
    assert b.total_value_eur == 0.0 and b.charge_eur == 0.0, "no value → no performance charge"
    print("✅ billing edge: zero value → zero charge (pure performance is risk-free)")


async def _async_paths():
    sim = PredictiveSimulator()
    r = await sim.simulate(AppointmentState(appointment_value=60, base_no_show_prob=0.4))
    b = await ValueBasedBilling(pct=0.1).bill([ValueEvent(kind="recovered_appointment", euros=60)])
    assert r.recommended != Action.NONE and b.charge_eur == 6.0
    print("✅ async API: simulate() + bill() via asyncio.to_thread")


def main():
    test_intervention_beats_doing_nothing()
    test_no_show_probability_drops_with_reminder()
    test_next_state_distribution_normalized()
    test_value_based_billing_and_roi()
    test_billing_edge_zero_value()
    asyncio.run(_async_paths())
    print("\n🎯 STEP 5 — Predictive Simulation + Value-Based Billing: ALL TESTS PASSED")


if __name__ == "__main__":
    main()
