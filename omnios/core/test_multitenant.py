"""
test_multitenant.py — 3 clinics on one platform: hard isolation, pinned keys, REAL calendar.
Proves: the platform serves 3 seeded clinics, every key is pinned to its clinic (A can never
read/write B, X-Clinic is ignored for non-admin keys), dates are real ISO dates (today/tomorrow),
'9:15' ≡ '09:15' is ONE physical slot, and each clinic's autopilot works only its own data.
Run: python core/test_multitenant.py
"""
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastapi.testclient import TestClient
from api import app
from clinic_store import today_iso

anon = TestClient(app)


def _clients():
    boot = anon.get("/auth/ui-key").json()
    assert boot["mode"] == "local-demo" and len(boot["clinics"]) == 3
    # SECURITY regression guard: /auth/ui-key must NOT leak other tenants' keys anymore.
    assert all("key" not in c for c in boot["clinics"]), "ui-key must not expose per-clinic keys!"
    # Tests legitimately need each tenant's key — read it from the internal registry, not the API.
    from api import _TENANT_KEYS
    return {c["tenant"]: (TestClient(app, headers={"X-API-Key": _TENANT_KEYS[c["tenant"]]}),
                          {**c, "key": _TENANT_KEYS[c["tenant"]]}) for c in boot["clinics"]}


def test_three_clinics_bootstrapped():
    cl = _clients()
    names = {meta["name"] for _, meta in cl.values()}
    assert {"Ιατρείο Ευεξία", "Καρδιολογικό Κέντρο Ηρακλείου", "Παιδιατρείο Χαμόγελο"} == names
    keys = {meta["key"] for _, meta in cl.values()}
    assert len(keys) == 3, "each clinic must have its OWN key"
    for t, (c, meta) in cl.items():
        j = c.get("/clinic").json()
        assert j["clinic"]["name"] == meta["name"]
        assert j["metrics"]["patients"] == 5 and j["metrics"]["appointments_today"] == 6
    print("✅ 3 clinics live, each with its own pinned key and its own seeded book (5 pts, 6 appts today)")


def test_hard_isolation_between_clinics():
    cl = _clients()
    cardio, _ = cl["cardio-irakleio"]
    demo, _ = cl["demo-clinic"]
    marker = f"ΜΟΝΟ-ΚΑΡΔΙΟ-{int(time.time())}"
    b = cardio.post("/clinic/appointments", json={"time": "19:45", "patient": marker})
    assert b.status_code == 200
    cardio_sees = [a["patient"] for a in cardio.get("/clinic/appointments").json()["appointments"]]
    demo_sees = [a["patient"] for a in demo.get("/clinic/appointments").json()["appointments"]]
    assert marker in cardio_sees and marker not in demo_sees, "tenant leak!"
    # patients never cross either
    demo_pats = {p["name"] for p in demo.get("/clinic/patients").json()["patients"]}
    assert "Σ. Μαρκάκης" not in demo_pats
    # cleanup: cancel the marker booking so reruns stay clean
    cardio.post(f"/clinic/appointments/{b.json()['id']}/cancel")
    print("✅ isolation: a cardiology booking is INVISIBLE to the GP clinic (and patients never cross)")


def test_keys_are_pinned_x_clinic_ignored():
    cl = _clients()
    demo, _ = cl["demo-clinic"]
    # a non-admin key CANNOT hop tenants via the X-Clinic header
    j = demo.get("/clinic", headers={"X-Clinic": "cardio-irakleio"}).json()
    assert j["clinic"]["name"] == "Ιατρείο Ευεξία", "X-Clinic must be ignored for pinned keys"
    print("✅ pinned keys: X-Clinic header is ignored unless the caller holds the admin key")


def test_real_calendar_dates():
    cl = _clients()
    demo, _ = cl["demo-clinic"]
    j = demo.get("/clinic/appointments").json()
    dates = {a["date"] for a in j["appointments"]}
    assert j["today"] == today_iso()
    assert today_iso() in dates, "seed must live on the REAL current date"
    assert any(d > today_iso() for d in dates), "tomorrow's bookings exist (real forward calendar)"
    today_only = demo.get(f"/clinic/appointments?date={today_iso()}").json()["appointments"]
    assert all(a["date"] == today_iso() for a in today_only) and len(today_only) >= 6
    print(f"✅ real calendar: seeds on {today_iso()} + tomorrow · ?date= filters a REAL day")


def test_time_normalization_one_physical_slot():
    cl = _clients()
    paido, _ = cl["paido-hamogelo"]
    ok = paido.post("/clinic/appointments", json={"time": "9:15", "patient": "Α"})
    assert ok.status_code == 200 and ok.json()["time"] == "09:15", "canonical HH:MM"
    dup = paido.post("/clinic/appointments", json={"time": "09:15", "patient": "Β"})
    assert dup.status_code == 409, "'9:15' and '09:15' are the SAME physical slot"
    bad = paido.post("/clinic/appointments", json={"time": "25:99", "patient": "Γ"})
    assert bad.status_code == 409
    paido.post(f"/clinic/appointments/{ok.json()['id']}/cancel")
    print("✅ time: '9:15'→'09:15' canonical, duplicate slot 409, garbage time rejected")


def test_autopilot_per_clinic():
    cl = _clients()
    cardio, _ = cl["cardio-irakleio"]
    r = cardio.post("/clinic/autopilot/scan").json()
    pend = cardio.get("/clinic/autopilot").json()["pending"]
    names = {p["patient"] for p in pend}
    assert names and all(("άκη" in n or "Μαρκ" in n or "Ψαρ" in n or "Φραγκ" in n or "Τζωρτζ" in n
                          or "Δασκ" in n or "Σταυρ" in n or "Βασιλ" in n) for n in names), names
    # the cardio no-show is worth the cardio session price (€60), not the GP's €40
    ns = [p for p in pend if p["type"] == "no_show_recovery"]
    assert ns and ns[0]["est_eur"] == 60.0
    print(f"✅ per-clinic autopilot: proposals only for cardio patients, €-at-risk = cardio price (€60)")


def main():
    test_three_clinics_bootstrapped()
    test_hard_isolation_between_clinics()
    test_keys_are_pinned_x_clinic_ignored()
    test_real_calendar_dates()
    test_time_normalization_one_physical_slot()
    test_autopilot_per_clinic()
    print("\n🎯 MULTI-TENANT PLATFORM — 3 clinics, hard isolation, real calendar: ALL TESTS PASSED")


if __name__ == "__main__":
    main()
