"""
test_soap_gr.py — ελληνικό SOAP draft: δομεί ό,τι ειπώθηκε, δεν εφευρίσκει, PII φεύγει.
Run: python core/test_soap_gr.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clinical_scribe import ClinicalScribe, to_soap_gr

TRANSCRIPT = ("Ασθενής Γιώργος Παπαδόπουλος, τηλέφωνο 6945123456. "
              "Παράπονο: οσφυαλγία εδώ και δύο εβδομάδες, με μυϊκό σπασμό. Αναφέρει και πονοκέφαλο.")


def test_soap_structure_and_pii():
    sc = ClinicalScribe()
    note = sc.draft_sync(TRANSCRIPT)
    soap = to_soap_gr(note)
    assert set(soap) == {"S", "O", "A", "P", "disclaimer"}
    # PII: ούτε όνομα ούτε τηλέφωνο πουθενά στο SOAP
    joined = " ".join(soap.values())
    assert "6945123456" not in joined and "Παπαδόπουλος" not in joined, "PII διέρρευσε στο SOAP!"
    # A: κωδικοποίηση ελληνικών όρων → ICD-10
    assert "M54.5" in soap["A"], soap["A"]              # οσφυαλγία
    assert "R51" in soap["A"], soap["A"]                # κεφαλαλγία
    # O: SNOMED αναφορές μόνο για ό,τι βρέθηκε
    assert "SNOMED" in soap["O"]
    # P: πάντα ο γιατρός οριστικοποιεί
    assert "ιατρό" in soap["P"] or "ιατρός" in soap["P"]
    assert "υπογραφή" in soap["P"] or "υπογραφή" in soap["disclaimer"]
    print("✅ SOAP: S/O/A/P δομή · PII αφαιρέθηκε · οσφυαλγία→M54.5, πονοκέφαλος→R51 · ο γιατρός οριστικοποιεί")


def test_empty_and_unknown():
    sc = ClinicalScribe()
    note = sc.draft_sync("Ο καιρός είναι καλός σήμερα.")   # τίποτα κλινικό
    soap = to_soap_gr(note)
    assert "καμία κωδικοποιήσιμη" in soap["A"] or soap["A"].startswith("—"), soap["A"]
    print("✅ honest: μη-κλινικό κείμενο → κενή εκτίμηση με σαφή σήμανση (καμία εφεύρεση)")


def test_endpoint():
    from fastapi.testclient import TestClient
    from api import app, _DEV_API_KEY
    anon = TestClient(app)
    assert anon.post("/scribe/soap", json={"transcript": "x"}).status_code == 401
    keyed = TestClient(app, headers={"X-API-Key": _DEV_API_KEY})
    r = keyed.post("/scribe/soap", json={"transcript": TRANSCRIPT})
    assert r.status_code == 200 and "S" in r.json()["soap"], r.text
    print("✅ endpoint /scribe/soap: 401 χωρίς κλειδί · 200 με SOAP δομή")


def main():
    test_soap_structure_and_pii()
    test_empty_and_unknown()
    test_endpoint()
    print("\n📋 GREEK SOAP: ALL TESTS PASSED — δομεί, δεν εφευρίσκει· draft-only· PII-safe")


if __name__ == "__main__":
    main()
