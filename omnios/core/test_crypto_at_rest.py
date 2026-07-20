"""
test_crypto_at_rest.py — αποδεικνύει encryption at rest για PII (GDPR Art.32).
Run: python core/test_crypto_at_rest.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import json
import os
import sqlite3
import tempfile

from store import KVStore
from clinic_store import ClinicStore


def _raw_values(path: str) -> list[str]:
    con = sqlite3.connect(path)
    rows = con.execute("SELECT value FROM kv").fetchall()
    con.close()
    return [r[0] for r in rows]


def test_pii_is_ciphertext_on_disk():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        cs = ClinicStore(path, tenant="clinicA")
        cs.add_patient(name="Γιώργος Παπαδόπουλος", phone="6945123456", amka="12036512345")
        cs.kv.close()
        raw = " ".join(_raw_values(path))
        # ΤΟ ΚΡΙΣΙΜΟ: ούτε το ΑΜΚΑ ούτε το τηλέφωνο ούτε το όνομα φαίνονται σε plaintext στο αρχείο
        assert "12036512345" not in raw, "ΑΜΚΑ βρέθηκε σε plaintext στον δίσκο!"
        assert "6945123456" not in raw, "τηλέφωνο σε plaintext!"
        assert "Παπαδόπουλος" not in raw, "όνομα σε plaintext!"
        assert "enc::v1::" in raw, "τα values δεν είναι κρυπτογραφημένα (λείπει prefix)"
        print("✅ ΑΜΚΑ/τηλέφωνο/όνομα ΔΕΝ είναι πια plaintext στο SQLite — μόνο ciphertext (enc::v1::)")
    finally:
        os.remove(path)


def test_roundtrip_and_restart():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        cs = ClinicStore(path, tenant="clinicB")
        p = cs.add_patient(name="Μαρία Ν.", phone="6971000002", amka="09085611111")
        cs.add_appointment("10:00", "Μαρία Ν.", date="2026-07-10")
        cs.kv.close()
        # νέο "process": τα δεδομένα επιβιώνουν ΚΑΙ αποκρυπτογραφούνται σωστά
        cs2 = ClinicStore(path, tenant="clinicB")
        got = cs2.get_patient(p["id"])
        assert got and got["amka"] == "09085611111" and got["name"] == "Μαρία Ν.", got
        assert len(cs2.list_appointments()) == 1
        cs2.kv.close()
        print("✅ round-trip + restart: κρυπτογραφείται στο put, αποκρυπτογραφείται σωστά στο get")
    finally:
        os.remove(path)


def test_legacy_plaintext_still_readable():
    """Backward-compat: παλιά plaintext rows (χωρίς prefix) πρέπει να διαβάζονται κανονικά."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        kv = KVStore(path)
        # γράψε ΧΕΙΡΟΚΙΝΗΤΑ ένα legacy plaintext row (όπως θα υπήρχε από πριν την κρυπτογράφηση)
        con = sqlite3.connect(path)
        con.execute("INSERT INTO kv(ns,key,value,updated_at) VALUES(?,?,?,datetime('now'))",
                    ("patient:old", "pat-legacy", json.dumps({"id": "pat-legacy", "name": "Παλιός"}, ensure_ascii=False)))
        con.commit(); con.close()
        got = kv.get("patient:old", "pat-legacy")
        assert got and got["name"] == "Παλιός", got
        kv.close()
        print("✅ legacy plaintext row διαβάζεται κανονικά (καμία υποχρεωτική migration)")
    finally:
        os.remove(path)


def main():
    test_pii_is_ciphertext_on_disk()
    test_roundtrip_and_restart()
    test_legacy_plaintext_still_readable()
    print("\n🔒 ENCRYPTION AT REST: ALL TESTS PASSED — PII κρυπτογραφημένο, backward-compatible")


if __name__ == "__main__":
    main()
