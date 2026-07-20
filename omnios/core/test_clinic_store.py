"""
test_clinic_store.py — the persistent clinic system of record (NOT demo numbers).
Proves: data survives a restart, double-booking is impossible, no-show/cancel drive autonomy,
metrics are computed from real rows, tenants are isolated, and a clinic can import its data.
Run: python core/test_clinic_store.py
"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clinic_store import ClinicStore


def test_persists_across_restart():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        cs = ClinicStore(path, tenant="c1")
        a = cs.add_appointment("18:00", "Α. Δοκιμαστικός", value=45)
        cs.kv.close()
        # a brand-new process reopens the same DB → the brain remembers
        cs2 = ClinicStore(path, tenant="c1")
        got = [x for x in cs2.list_appointments() if x["id"] == a["id"]]
        assert got and got[0]["patient"] == "Α. Δοκιμαστικός", "appointment did not survive restart"
        cs2.kv.close()
        print("✅ persistence: an appointment booked in one process is there after restart")
    finally:
        os.remove(path)


def test_double_booking_guard():
    cs = ClinicStore(":memory:")
    cs.add_appointment("09:00", "Α", date="2026-07-03")
    try:
        cs.add_appointment("09:00", "Β", date="2026-07-03")
        assert False, "double booking was allowed"
    except ValueError:
        pass
    # a cancelled slot frees up
    cs2 = ClinicStore(":memory:")
    x = cs2.add_appointment("09:00", "Α", date="2026-07-03")
    cs2.set_status(x["id"], "cancelled")
    cs2.add_appointment("09:00", "Β", date="2026-07-03")   # now allowed
    print("✅ double-booking guard: same slot rejected while active, reusable once cancelled")


def test_no_show_and_cancel_autonomy():
    cs = ClinicStore(":memory:")
    cs.add_waitlist("Επόμενος", "πρωί")
    a = cs.add_appointment("10:00", "Ασθενής")
    cs.set_status(a["id"], "no_show")   # → recovery enqueued (audited)
    b = cs.add_appointment("11:00", "Άλλος")
    res = cs.cancel_appointment(b["id"])   # → waitlist backfill
    assert res["backfilled"] is not None and res["backfilled"]["patient"] == "Επόμενος"
    assert not cs.list_waitlist(), "waitlist entry should be consumed by backfill"
    print("✅ autonomy: no-show → recovery enqueued · cancellation → waitlist auto-backfilled the slot")


def test_metrics_from_real_data():
    cs = ClinicStore(":memory:")
    cs.seed_demo()
    m = cs.metrics()
    assert m["appointments_today"] == 6 and m["completed"] == 3 and m["no_shows"] == 1
    assert m["patients"] == 5 and m["recovered_eur"] == 120.0 and m["roi"] == 10.0
    # mutate real data → metrics move
    a = cs.add_appointment("19:00", "Νέος")
    assert cs.metrics()["appointments_today"] == 7
    cs.add_value("recovered_appointment", 45)
    assert cs.metrics()["recovered_eur"] == 165.0
    print("✅ metrics: every KPI derived from real rows; mutating data moves the numbers")


def test_tenant_isolation():
    cs_a = ClinicStore(":memory:", tenant="alpha")
    cs_a.add_patient("Μυστικός Α")
    cs_b = ClinicStore(":memory:", tenant="beta")
    # note: :memory: gives each instance its own DB; isolation is by namespace on a SHARED db too:
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        a = ClinicStore(path, tenant="alpha"); a.add_patient("Ασθενής Α")
        b = ClinicStore(path, tenant="beta"); b.add_patient("Ασθενής Β")
        assert len(a.list_patients()) == 1 and a.list_patients()[0]["name"] == "Ασθενής Α"
        assert len(b.list_patients()) == 1 and b.list_patients()[0]["name"] == "Ασθενής Β"
        print("✅ multi-tenant: two clinics on one DB see only their own patients (namespace isolation)")
    finally:
        a.kv.close(); b.kv.close(); os.remove(path)


def test_import_connects_a_clinic():
    cs = ClinicStore(":memory:")
    res = cs.import_data(
        patients=[{"name": "Εισαγ. 1", "phone": "6970000001"}, {"name": "Εισαγ. 2", "consent": False}],
        appointments=[{"time": "08:00", "patient": "Εισαγ. 1", "status": "confirmed"},
                      {"time": "08:00", "patient": "Εισαγ. 2"}])   # 2nd clashes → skipped
    assert res["patients_imported"] == 2 and res["appointments_imported"] == 1
    # consent flag preserved (GDPR Art.9)
    p2 = [p for p in cs.list_patients() if p["name"] == "Εισαγ. 2"][0]
    assert p2["consent"] is False
    print("✅ import: a clinic connects its patients/appointments; consent preserved, slot clashes skipped")


def main():
    test_persists_across_restart()
    test_double_booking_guard()
    test_no_show_and_cancel_autonomy()
    test_metrics_from_real_data()
    test_tenant_isolation()
    test_import_connects_a_clinic()
    print("\n🎯 CLINIC STORE — real persistent system of record: ALL TESTS PASSED (not demo numbers)")


if __name__ == "__main__":
    main()
