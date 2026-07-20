"""
test_clinic_api.py — the REAL clinic operations over HTTP (persisted, drive every number).
Run: python core/test_clinic_api.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastapi.testclient import TestClient
from api import app, _DEV_API_KEY

c = TestClient(app, headers={"X-API-Key": _DEV_API_KEY})   # clinic ops are SECURED (Art.32)


def test_clinic_requires_auth_and_ui_bootstrap():
    anon = TestClient(app)
    assert anon.post("/clinic/appointments", json={"time": "09:00", "patient": "x"}).status_code == 401
    assert anon.get("/clinic/patients").status_code == 401, "patient PII must not be readable anonymously"
    # local demo bootstrap: /auth/ui-key hands the UI a key ONLY when no OMNIOS_ADMIN_KEY is set
    boot = anon.get("/auth/ui-key")
    assert boot.status_code == 200 and boot.json()["key"], "local mode must bootstrap the UI"
    keyed = TestClient(app, headers={"X-API-Key": boot.json()["key"]})
    assert keyed.get("/clinic/patients").status_code == 200
    print("✅ security: clinic reads/writes 401 without key · UI bootstraps via /auth/ui-key (local mode)")


def test_clinic_seeded_and_metrics_live():
    r = c.get("/clinic")
    assert r.status_code == 200, r.text
    m = r.json()["metrics"]
    assert m["patients"] >= 5 and m["appointments_today"] >= 6
    # /metrics mirrors the clinic store (real ops numbers, not constants)
    gm = c.get("/metrics").json()
    assert gm["appointments_today"] == m["appointments_today"] and gm["recovered_eur"] == m["recovered_eur"]
    print(f"✅ /clinic seeded · metrics live: {m['appointments_today']} appts, {m['patients']} patients, €{m['recovered_eur']} recovered")


def test_book_persists_and_guards_double_booking():
    before = len(c.get("/clinic/appointments").json()["appointments"])
    b = c.post("/clinic/appointments", json={"time": "18:30", "patient": "Δοκιμή Α", "date": "today"})
    assert b.status_code == 200, b.text
    after = c.get("/clinic/appointments").json()["appointments"]
    assert len(after) == before + 1 and any(a["patient"] == "Δοκιμή Α" for a in after)
    dup = c.post("/clinic/appointments", json={"time": "18:30", "patient": "Δοκιμή Β", "date": "today"})
    assert dup.status_code == 409, "double-booking must be rejected"
    print("✅ POST /clinic/appointments → persists in the list · duplicate slot → 409 (guard)")


def test_no_show_and_cancel_backfill():
    c.post("/clinic/waitlist", json={"name": "Αναμονής Χ", "note": "πρωί"})
    wl = c.get("/clinic/appointments").json()["waitlist"]
    head = wl[0]["name"]   # FIFO: the longest-waiting patient gets the freed slot
    a = c.post("/clinic/appointments", json={"time": "19:15", "patient": "Θα λείψει"}).json()
    ns = c.post(f"/clinic/appointments/{a['id']}/status", json={"status": "no_show"})
    assert ns.status_code == 200 and ns.json()["status"] == "no_show"
    b = c.post("/clinic/appointments", json={"time": "19:45", "patient": "Θα ακυρώσει"}).json()
    cancel = c.post(f"/clinic/appointments/{b['id']}/cancel").json()
    assert cancel["backfilled"] and cancel["backfilled"]["patient"] == head, "backfill must take the FIFO head"
    print("✅ no-show recorded · cancellation auto-backfilled the longest-waiting patient (FIFO, real data)")


def test_patients_and_invoices():
    p = c.post("/clinic/patients", json={"name": "Νέα Ασθενής", "phone": "6970000009", "consent": True})
    assert p.status_code == 200 and p.json()["consent"] is True
    assert any(x["name"] == "Νέα Ασθενής" for x in c.get("/clinic/patients").json()["patients"])
    inv = c.post("/clinic/invoices", json={"patient": "Νέα Ασθενής", "amount": 30}).json()
    paid = c.post(f"/clinic/invoices/{inv['id']}/pay")
    assert paid.status_code == 200 and paid.json()["status"] == "paid"
    print("✅ patients add (consent kept) · invoice issue → pay, all persisted")


def test_import_connects_data():
    r = c.post("/clinic/import", json={
        "patients": [{"name": "Import Πελ. 1", "phone": "6970001111"}],
        "appointments": [{"time": "07:30", "patient": "Import Πελ. 1", "status": "confirmed"}]})
    assert r.status_code == 200 and r.json()["patients_imported"] == 1 and r.json()["appointments_imported"] == 1
    print("✅ POST /clinic/import → a clinic connects its own data; system operates on it")




def test_patient_edit_waitlist_remove_and_notes():
    # edit phone + consent (audited)
    p = c.post("/clinic/patients", json={"name": "Επεξ. Τεστ", "phone": "6970000000"}).json()
    upd = c.post(f"/clinic/patients/{p['id']}", json={"phone": "6979999999", "consent": False})
    assert upd.status_code == 200 and upd.json()["phone"] == "6979999999" and upd.json()["consent"] is False
    assert c.post("/clinic/patients/pat-nope", json={"phone": "1"}).status_code == 404
    # waitlist add → remove
    w = c.post("/clinic/waitlist", json={"name": "Προς Αφαίρεση"}).json()
    c.post(f"/clinic/waitlist/{w['id']}/remove")
    names = [x["name"] for x in c.get("/clinic/appointments").json()["waitlist"]]
    assert "Προς Αφαίρεση" not in names
    # signed note lands in the persisted store notes
    d = c.post("/scribe/draft", json={"transcript": "Ασθενής με οσφυαλγία."}).json()
    c.post("/scribe/sign", json={"draft_id": d["id"], "clinician_id": "dr_x"})
    notes = c.get("/clinic/notes").json()["notes"]
    assert any(n.get("signed_by") == "dr_x" for n in notes)
    print("✅ patient edit (phone/consent audited) · waitlist remove · signed notes listed from store")

def main():
    test_clinic_requires_auth_and_ui_bootstrap()
    test_clinic_seeded_and_metrics_live()
    test_book_persists_and_guards_double_booking()
    test_no_show_and_cancel_backfill()
    test_patients_and_invoices()
    test_import_connects_data()
    test_patient_edit_waitlist_remove_and_notes()
    print("\n🎯 CLINIC OPERATIONS API — real, persisted, autonomous over live data: ALL TESTS PASSED")


if __name__ == "__main__":
    main()
