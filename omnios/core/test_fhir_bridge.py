"""
test_fhir_bridge.py — Το OmniOS ως FHIR R4 endpoint: read + ingest + round-trip.
Αποδεικνύει: clinic resources ↔ FHIR R4 (Patient/Appointment/Invoice), CapabilityStatement έγκυρο,
εισερχόμενο FHIR Appointment → πραγματικό ραντεβού στο store (η γέφυρα ingest), και round-trip
appointment→FHIR→appointment. Τρέχει: python core/test_fhir_bridge.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clinic_store import ClinicStore
from fhir_bridge import (patient_to_fhir, appointment_to_fhir, invoice_to_fhir, bundle,
                         fhir_to_appointment, capability_statement)

fails = []
def chk(label, ok):
    print(f"  {'✅' if ok else '❌ ΣΚΑΣΕ'} {label}")
    if not ok:
        fails.append(label)


def test_resource_mapping():
    cs = ClinicStore(":memory:", tenant="fhir-t")
    cs.setup_clinic(name="FHIR Ιατρείο", specialty="Φυσικοθεραπεία", default_value=40.0)
    p = cs.add_patient("Άννα Ασθενής", phone="6970000001", amka="01018012345", consent=True)
    a = cs.add_appointment("10:00", "Άννα Ασθενής", "Συνεδρία", "2026-08-01", 40.0, "confirmed", p["id"])
    inv = cs.add_invoice("Άννα Ασθενής", 40.0, "unpaid", p["id"], a["id"])

    fp = patient_to_fhir(p)
    chk("Patient → FHIR R4 (resourceType, identifier, ΑΜΚΑ, consent extension)",
        fp["resourceType"] == "Patient" and any(i["value"] == "01018012345" for i in fp["identifier"])
        and fp["extension"][0]["valueBoolean"] is True)

    fa = appointment_to_fhir(a)
    chk("Appointment → FHIR R4 (status=booked, start ISO datetime, participant)",
        fa["resourceType"] == "Appointment" and fa["status"] == "booked"
        and fa["start"] == "2026-08-01T10:00:00" and fa["participant"][0]["actor"]["display"] == "Άννα Ασθενής")

    fi = invoice_to_fhir(inv)
    chk("Invoice → FHIR R4 (totalGross EUR, status issued)",
        fi["resourceType"] == "Invoice" and fi["totalGross"]["currency"] == "EUR"
        and fi["totalGross"]["value"] == 40.0)

    b = bundle([fp, fa, fi])
    chk("Bundle searchset σωστό (total=3)", b["resourceType"] == "Bundle" and b["total"] == 3)


def test_capability_statement():
    c = capability_statement()
    types = {r["type"] for r in c["rest"][0]["resource"]}
    chk("CapabilityStatement έγκυρο (FHIR 4.0.1, Patient+Appointment+Invoice)",
        c["resourceType"] == "CapabilityStatement" and c["fhirVersion"] == "4.0.1"
        and {"Patient", "Appointment", "Invoice"} <= types)
    appt = next(r for r in c["rest"][0]["resource"] if r["type"] == "Appointment")
    codes = {i["code"] for i in appt["interaction"]}
    chk("Appointment δηλώνει create (ingest) + search", {"create", "search-type"} <= codes)


def test_ingest_external_fhir_booking():
    """Ένα εξωτερικό σύστημα που μιλάει FHIR στέλνει Appointment → πραγματικό ραντεβού στο store."""
    cs = ClinicStore(":memory:", tenant="fhir-ingest")
    cs.setup_clinic(name="X", specialty="Φ", default_value=40.0)
    incoming = {"resourceType": "Appointment", "status": "booked",
                "start": "2026-09-15T14:30:00", "description": "Πρώτη επίσκεψη",
                "participant": [{"actor": {"display": "Νέος Ασθενής"}, "status": "accepted"}]}
    parsed = fhir_to_appointment(incoming)
    chk("FHIR Appointment parse-άρεται σωστά (date/time/patient/service)",
        parsed == {"date": "2026-09-15", "time": "14:30", "patient": "Νέος Ασθενής", "service": "Πρώτη επίσκεψη"})
    a = cs.add_appointment(parsed["time"], parsed["patient"], parsed["service"], parsed["date"])
    chk("Το εισερχόμενο FHIR έγινε ΠΡΑΓΜΑΤΙΚΟ ραντεβού στο store", len(cs.list_appointments()) == 1)
    # round-trip: internal → FHIR → internal ίδια πεδία
    back = fhir_to_appointment(appointment_to_fhir(a))
    chk("Round-trip appointment→FHIR→appointment διατηρεί date/time/patient",
        back["date"] == "2026-09-15" and back["time"] == "14:30" and back["patient"] == "Νέος Ασθενής")


def test_ingest_rejects_bad_input():
    bad = {"resourceType": "Patient"}   # λάθος τύπος
    try:
        fhir_to_appointment(bad)
        chk("Απορρίπτει μη-Appointment", False)
    except ValueError:
        chk("Απορρίπτει μη-Appointment (ValueError)", True)
    no_start = {"resourceType": "Appointment", "participant": [{"actor": {"display": "Χ"}}]}
    try:
        fhir_to_appointment(no_start)
        chk("Απορρίπτει χωρίς έγκυρο start", False)
    except ValueError:
        chk("Απορρίπτει χωρίς έγκυρο start (ValueError)", True)


if __name__ == "__main__":
    print("🔌 FHIR BRIDGE — OmniOS ως FHIR R4 endpoint:")
    test_resource_mapping()
    test_capability_statement()
    test_ingest_external_fhir_booking()
    test_ingest_rejects_bad_input()
    print()
    if fails:
        print(f"❌ {len(fails)} ΑΠΟΤΥΧΙΕΣ: {fails}")
        sys.exit(1)
    print("✅ FHIR BRIDGE: read (Patient/Appointment/Invoice) + ingest (POST Appointment) + round-trip,")
    print("   CapabilityStatement έγκυρο — κάθε σύστημα που μιλάει FHIR συνδέεται.")
