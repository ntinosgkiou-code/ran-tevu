"""
test_autopilot_hardening.py — regression tests for the 5 defects the deep audit found (4/7).
Each existed because the prior suite tested only the happy path. Run: python core/test_autopilot_hardening.py
"""
import sys, asyncio
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from concurrent.futures import ThreadPoolExecutor
from clinic_store import ClinicStore
from clinic_autopilot import ClinicAutopilot
from omnios_core import OmniosCore


def test_approve_is_idempotent():
    """A double-click / retry must NOT fire a second real SMS or issue a duplicate invoice."""
    cs = ClinicStore(":memory:"); cs.seed_demo()
    ap = ClinicAutopilot(cs); ap.sweep()
    prop = next(p for p in ap.pending() if p["type"] in ("no_show_recovery", "dunning", "reminder"))
    for _ in range(3):
        ap.approve(prop["id"], "dr")           # three approvals of the same proposal
    dispatched = [m for m in ap.channels.outbox() if prop["action"] in str(m)]
    assert len(dispatched) == 1, f"expected 1 dispatch, got {len(dispatched)} (side-effect ran twice)"
    # CONCURRENT double-click: 12 threads approve the SAME fresh proposal → still exactly one send
    from concurrent.futures import ThreadPoolExecutor
    cs3 = ClinicStore(":memory:"); cs3.seed_demo()
    ap3 = ClinicAutopilot(cs3); ap3.sweep()
    prop3 = next(p for p in ap3.pending() if p["type"] in ("no_show_recovery", "dunning"))
    n0 = len(ap3.channels.outbox())
    with ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(lambda _: ap3.approve(prop3["id"], "dr"), range(12)))
    fired = len(ap3.channels.outbox()) - n0
    assert fired == 1, f"concurrent approve fired {fired} sends (must be exactly 1)"
    # a billing proposal must issue exactly one invoice too
    cs2 = ClinicStore(":memory:"); cs2.seed_demo()
    ap2 = ClinicAutopilot(cs2); ap2.sweep()
    bill = next((p for p in ap2.pending() if p["type"] == "billing"), None)
    if bill:
        n0 = len(cs2.list_invoices())
        ap2.approve(bill["id"], "dr"); ap2.approve(bill["id"], "dr")
        assert len(cs2.list_invoices()) == n0 + 1, "duplicate invoice on re-approve"
    print("✅ approve() idempotent: 3 clicks → exactly 1 side-effect")


def test_consent_gate_uses_patient_id_not_name():
    """GDPR Art.9: two patients sharing a name with different consent must not cross —
    outreach for the NON-consenting one must be blocked even though a namesake consented."""
    cs = ClinicStore(":memory:"); cs.setup_clinic(name="T", specialty="Φυσικοθεραπεία")
    cs.add_patient("Γιώργος Παπαδόπουλος", phone="6900000001", consent=True)   # consents
    p2 = cs.add_patient("Γιώργος Παπαδόπουλος", phone="6900000002", consent=False)  # does NOT
    cs.add_appointment("10:00", "Γιώργος Παπαδόπουλος", date="2026-08-01",
                       status="no_show", patient_id=p2["id"])
    ap = ClinicAutopilot(cs); ap.sweep()
    outreach = [p for p in ap.pending() if p["type"] == "no_show_recovery"]
    assert not outreach, "outreach proposed for a patient who explicitly withheld consent"
    # sanity: if the SAME appt were for the consenting patient, outreach IS proposed
    cs.add_appointment("11:00", "Μόνη Ασθενής", date="2026-08-01", status="no_show",
                       patient_id=cs.add_patient("Μόνη Ασθενής", consent=True)["id"])
    ap.sweep()
    assert any(p["type"] == "no_show_recovery" for p in ap.pending()), "consenting patient was skipped"
    print("✅ consent gate keys on patient_id — namesakes don't leak consent")


def test_double_booking_is_race_safe():
    """30 concurrent bookings for the same slot → exactly ONE wins (atomic check+insert)."""
    cs = ClinicStore(":memory:"); cs.setup_clinic(name="T", specialty="x")

    def book(i):
        try:
            cs.add_appointment("09:00", f"P{i}", date="2026-09-01"); return 1
        except ValueError:
            return 0

    with ThreadPoolExecutor(max_workers=16) as ex:
        wins = sum(ex.map(book, range(30)))
    booked = [a for a in cs.list_appointments() if a["date"] == "2026-09-01" and a["time"] == "09:00"]
    assert wins == 1 and len(booked) == 1, f"double-booked: {wins} wins, {len(booked)} rows"
    print("✅ double-booking guard is atomic under 30-way concurrency")


def test_controlled_substance_freezes():
    """A controlled-substance mention always FREEZES for human review (manifest guardrail)."""
    core = OmniosCore()
    frozen = asyncio.run(core.run_note(
        "Ασθενής με ΑΜΚΑ 12036512345. Χορηγήθηκε τραμαδόλ. Αρτηριακή υπέρταση. Διάρκεια 30 λεπτά.",
        patient_type="established"))
    fails = [f["rule_id"] for f in frozen.rule_failures]
    assert "CONTROLLED-SUBSTANCE-REVIEW" in fails and frozen.phase.value == "HUMAN_OVERRIDE"
    clean = asyncio.run(core.run_note(
        "Ασθενής με ΑΜΚΑ 12036512345. Αρτηριακή υπέρταση, ρυθμισμένη. ΗΚΓ ρουτίνας. Διάρκεια 30 λεπτά.",
        patient_type="established"))
    assert clean.phase.value == "DONE", "clean note should still complete"
    print("✅ controlled-substance mention freezes; clean note completes")


def test_merkle_showcase_has_no_tenant_data():
    """The public Merkle endpoints must carry no tenant/name/specialty — and proofs still verify."""
    from fastapi.testclient import TestClient
    import api
    c = TestClient(api.app)
    b = c.get("/audit/merkle").json()
    assert b.get("root") and b["size"] >= 1
    for seq in range(min(b["size"], 25)):
        pr = c.get(f"/audit/merkle/proof/{seq}")
        if pr.status_code != 200:
            continue
        assert pr.json()["verified"] is True, f"proof {seq} broke"
        data = pr.json()["record"].get("data", {})
        assert not ({"tenant", "name", "specialty"} & set(data)), f"leak at seq {seq}: {data}"
    print("✅ Merkle showcase carries no tenant-identifying data; proofs verify")


def main():
    test_approve_is_idempotent()
    test_consent_gate_uses_patient_id_not_name()
    test_double_booking_is_race_safe()
    test_controlled_substance_freezes()
    test_merkle_showcase_has_no_tenant_data()
    print("\n🎯 AUTOPILOT & CORE HARDENING (audit 4/7): ALL TESTS PASSED")


if __name__ == "__main__":
    main()
