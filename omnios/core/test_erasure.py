"""
test_erasure.py — GDPR Art.17: σβήνει PII, κρατά ψευδωνυμοποιημένα τιμολόγια (εφορία).
Run: python core/test_erasure.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import os
import sqlite3
import tempfile

from clinic_store import ClinicStore


def test_erase_removes_pii_keeps_invoices():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        cs = ClinicStore(path, tenant="erase")
        p = cs.add_patient(name="Ελένη Ερασέ", phone="6970000009", amka="15057788990")
        cs.add_appointment("10:00", "Ελένη Ερασέ", date="2026-07-15", patient_id=p["id"])
        inv = cs.add_invoice("Ελένη Ερασέ", 40.0, "unpaid", p["id"])
        # πράξη διαγραφής
        res = cs.erase_patient(p["id"])
        assert res and res["invoices_pseudonymized"] == 1 and res["appointments_pseudonymized"] == 1, res
        # 1) ο ασθενής έφυγε εντελώς
        assert cs.get_patient(p["id"]) is None
        assert all(x["name"] != "Ελένη Ερασέ" for x in cs.list_patients())
        # 2) το τιμολόγιο ΜΕΝΕΙ (ποσό/status για εφορία) αλλά ψευδωνυμοποιημένο
        invs = cs.list_invoices()
        assert len(invs) == 1 and invs[0]["amount"] == 40.0 and "διαγραμμένος" in invs[0]["patient"], invs
        # 3) το ραντεβού ΜΕΝΕΙ (slot) αλλά χωρίς ταυτότητα
        appts = cs.list_appointments()
        assert len(appts) == 1 and "διαγραμμένος" in appts[0]["patient"] and appts[0]["patient_id"] == ""
        # 4) ΤΟ ΚΡΙΣΙΜΟ: το ΑΜΚΑ/τηλέφωνο δεν υπάρχει ΠΟΥΘΕΝΑ πια στον δίσκο (ούτε κρυπτογραφημένο)
        cs.kv.close()
        con = sqlite3.connect(path)
        raw = " ".join(r[0] for r in con.execute("SELECT value FROM kv").fetchall())
        con.close()
        # decrypt-check: κανένα value δεν αποκρυπτογραφείται σε ΑΜΚΑ/τηλέφωνο
        from crypto_at_rest import decrypt
        allplain = " ".join(decrypt(v) for v in raw.split())
        assert "15057788990" not in allplain and "6970000009" not in allplain, "PII επέζησε της διαγραφής!"
        print("✅ διαγραφή: ΑΜΚΑ/τηλέφωνο/όνομα έφυγαν εντελώς· τιμολόγιο+slot έμειναν ψευδωνυμοποιημένα")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def test_endpoint_erase():
    from fastapi.testclient import TestClient
    from api import app, _DEV_API_KEY
    c = TestClient(app, headers={"X-API-Key": _DEV_API_KEY})
    # φτιάξε έναν ασθενή μέσω API και σβήσ' τον
    pid = c.post("/clinic/patients", json={"name": "Προς Διαγραφή", "amka": "20011122334"}).json()["id"]
    assert c.post(f"/clinic/patients/{pid}/erase").status_code == 200
    assert c.post("/clinic/patients/does-not-exist/erase").status_code == 404
    print("✅ endpoint: 200 σε διαγραφή, 404 σε ανύπαρκτο")


def main():
    test_erase_removes_pii_keeps_invoices()
    test_endpoint_erase()
    print("\n🧹 GDPR ERASURE (Art.17): ALL TESTS PASSED")


if __name__ == "__main__":
    main()
