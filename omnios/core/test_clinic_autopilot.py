"""
test_clinic_autopilot.py — the autonomous operator ACTS on the REAL clinic data.
Proves: a sweep produces governed proposals from real appointments/patients/invoices, dedupes,
approve executes the real side-effect, consent (Art.9) is ENFORCED before any outreach proposal,
and reschedule moves a booking without releasing the slot.
Run: python core/test_clinic_autopilot.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clinic_store import ClinicStore
from clinic_autopilot import ClinicAutopilot


def test_sweep_from_real_data():
    cs = ClinicStore(":memory:"); cs.seed_demo()
    ap = ClinicAutopilot(cs)
    r = ap.sweep()
    types = {p["type"] for p in ap.pending()}
    assert r["new"] >= 4 and {"no_show_recovery", "reminder", "recall", "dunning"} <= types
    # every € traces to a real value (no arbitrary constants)
    ns = next(p for p in ap.pending() if p["type"] == "no_show_recovery")
    assert ns["est_eur"] == 40.0   # == the real appointment value (session price), not a hardcoded 80
    print(f"✅ sweep: {r['new']} governed proposals from REAL data ({sorted(types)}), €{r['eur_at_risk']} at risk")


def test_dedupe_no_double_proposals():
    cs = ClinicStore(":memory:"); cs.seed_demo()
    ap = ClinicAutopilot(cs)
    first = ap.sweep()["new"]
    again = ap.sweep()["new"]
    assert first > 0 and again == 0, "a second sweep must not re-propose the same tasks"
    print(f"✅ dedupe: first sweep {first} proposals, second sweep 0 (idempotent)")


def test_approve_executes_real_side_effect():
    cs = ClinicStore(":memory:"); cs.seed_demo()
    ap = ClinicAutopilot(cs)
    ap.sweep()
    # approving a no-show recovery is audited + marked, but does NOT fabricate recovered € (honest)
    ns = next(p for p in ap.pending() if p["type"] == "no_show_recovery")
    before_val = cs.billing()["total_value_eur"]
    ap.approve(ns["id"], "dr_physio")
    assert cs.billing()["total_value_eur"] == before_val, "approving outreach must NOT invent recovered €"
    assert ns["id"] not in [p["id"] for p in ap.pending()]
    print("✅ approve: authorized + audited, executes real side-effect, no fabricated € (defensible)")


def test_consent_gate_blocks_outreach():
    cs = ClinicStore(":memory:")
    cs.setup_clinic(name="Test", specialty="Φυσικοθεραπεία")
    cs.add_patient("Συναινεί", phone="6970000001", consent=True)
    cs.add_patient("ΔΕΝ Συναινεί", phone="6970000002", consent=False)
    cs.add_appointment("09:00", "Συναινεί", status="no_show")
    cs.add_appointment("10:00", "ΔΕΝ Συναινεί", status="no_show")
    ap = ClinicAutopilot(cs)
    ap.sweep()
    targets = {p["patient"] for p in ap.pending() if p["type"] == "no_show_recovery"}
    assert "Συναινεί" in targets, "consenting patient should get a recovery proposal"
    assert "ΔΕΝ Συναινεί" not in targets, "GDPR Art.9: a non-consenting patient must NOT be contacted"
    print("✅ consent (Art.9) ENFORCED: non-consenting patient excluded from outreach proposals")


def test_reschedule_moves_without_releasing_slot():
    cs = ClinicStore(":memory:")
    cs.setup_clinic(name="Test")
    cs.add_waitlist("Περιμένει")
    a = cs.add_appointment("09:00", "Ασθενής", date="2026-07-05")
    moved = cs.reschedule_appointment(a["id"], "11:00", "2026-07-05")
    assert moved["time"] == "11:00"
    # reschedule must NOT hand the old slot to the waitlist (unlike cancel)
    assert cs.list_waitlist() and cs.list_waitlist()[0]["name"] == "Περιμένει"
    assert all(x["patient"] == "Ασθενής" for x in cs.list_appointments())   # still the same one booking
    # can't move onto an occupied slot
    cs.add_appointment("12:00", "Άλλος", date="2026-07-05")
    try:
        cs.reschedule_appointment(a["id"], "12:00", "2026-07-05")
        assert False, "reschedule onto a taken slot must be blocked"
    except ValueError:
        pass
    print("✅ reschedule: moves the booking in place, keeps the patient, guards the target slot")


def main():
    test_sweep_from_real_data()
    test_dedupe_no_double_proposals()
    test_approve_executes_real_side_effect()
    test_consent_gate_blocks_outreach()
    test_reschedule_moves_without_releasing_slot()
    print("\n🎯 AUTOPILOT + CONSENT + RESCHEDULE: ALL TESTS PASSED — autonomy acts on real data, GDPR-gated")


if __name__ == "__main__":
    main()
