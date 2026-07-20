"""
test_fhir_bridge.py (Ran-Tevu) — το ιατρείο ως FHIR R4 endpoint: mapping + ingest + endpoints.
Τρέχει: python tests/test_fhir_bridge.py
"""
import os, sys, tempfile
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "rt_fhir.db")
if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])
from cryptography.fernet import Fernet
os.environ["RANTEVU_ENC_KEY"] = Fernet.generate_key().decode()
os.environ["ADMIN_TOKEN"] = "fhir-test-admin"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import database as db
import provisioning
import fhir_bridge as F
from datetime import datetime, timedelta

fails = []
def chk(label, ok):
    print(f"  {'✅' if ok else '❌ ΣΚΑΣΕ'} {label}")
    if not ok:
        fails.append(label)

TOM = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


def test_mapping_and_ingest():
    b = {"id": 7, "slug": "x", "date": "2026-08-01", "time": "10:00", "duration": 30,
         "service": "Συνεδρία", "name": "Άννα Α.", "phone": "6970000001", "status": "confirmed"}
    fa = F.booking_to_fhir(b)
    chk("booking → FHIR Appointment (status=booked, start ISO, participant)",
        fa["resourceType"] == "Appointment" and fa["status"] == "booked"
        and fa["start"] == "2026-08-01T10:00:00" and fa["participant"][0]["actor"]["display"] == "Άννα Α.")

    p = {"phone": "6970000001", "name": "Άννα Α.", "email": "a@x.gr", "amka": "01018012345", "status": "active"}
    fp = F.patient_to_fhir(p)
    chk("bo_patient → FHIR Patient (ΑΜΚΑ identifier, telecom)",
        fp["resourceType"] == "Patient" and any(i["value"] == "01018012345" for i in fp["identifier"]))

    inv = {"id": 3, "patient_phone": "6970000001", "patient_name": "Άννα Α.", "total": 40.0, "status": "unpaid",
           "mydata_status": "pending"}
    fi = F.invoice_to_fhir(inv)
    chk("bo_invoice → FHIR Invoice (totalGross EUR, myDATA extension)",
        fi["resourceType"] == "Invoice" and fi["totalGross"]["value"] == 40.0)

    incoming = {"resourceType": "Appointment", "status": "booked", "start": "2026-09-15T14:30:00",
                "minutesDuration": 30, "description": "Πρώτη επίσκεψη",
                "participant": [{"actor": {"reference": "Patient/6970000009", "display": "Νέος Ασθενής"}}]}
    parsed = F.fhir_to_booking(incoming)
    chk("FHIR Appointment → internal (date/time/name/phone/service)",
        parsed["date"] == "2026-09-15" and parsed["time"] == "14:30"
        and parsed["name"] == "Νέος Ασθενής" and parsed["phone"] == "6970000009")


def test_endpoints():
    db.init_db()
    import app as A
    from routes.dashboard_routes import dash_token
    prov = provisioning.provision("FHIR Ιατρείο", "physio", owner_name="Δρ", email="dr@x.gr", demo=False)
    S = prov["slug"]
    tok = dash_token(S)
    client = A.app.test_client()

    # metadata (public)
    r = client.get("/fhir/metadata")
    chk("GET /fhir/metadata → CapabilityStatement 4.0.1", r.status_code == 200 and r.get_json()["fhirVersion"] == "4.0.1")

    # gate: χωρίς token → 403
    chk("Read χωρίς token → 403", client.get(f"/fhir/{S}/Appointment").status_code == 403)

    # ingest external FHIR booking
    fhir_appt = {"resourceType": "Appointment", "status": "booked", "start": f"{TOM}T11:00:00",
                 "description": "Consult", "participant": [{"actor": {"reference": "Patient/6970001234", "display": "FHIR Ασθενής"}}]}
    r = client.post(f"/fhir/{S}/Appointment?t={tok}", json=fhir_appt)
    chk("POST /fhir/<slug>/Appointment (ingest) → 201 + FHIR Appointment",
        r.status_code == 201 and r.get_json()["resourceType"] == "Appointment")

    # read back
    r = client.get(f"/fhir/{S}/Appointment?t={tok}")
    j = r.get_json()
    chk("GET /fhir/<slug>/Appointment → Bundle με το ingested ραντεβού",
        r.status_code == 200 and j["total"] == 1 and j["entry"][0]["resource"]["participant"][0]["actor"]["display"] == "FHIR Ασθενής")

    # double-booking on ingest → 409
    r = client.post(f"/fhir/{S}/Appointment?t={tok}", json=fhir_appt)
    chk("Ingest ίδιου slot → 409 (double-booking guard)", r.status_code == 409)

    # bridges/status honest labels
    r = client.get(f"/api/{S}/bridges/status?t={tok}")
    states = {b["name"]: b["state"] for b in r.get_json()["bridges"]}
    chk("bridges/status: FHIR=LIVE, ΗΔΙΚΑ=no_public_api (τίμιοι χαρακτηρισμοί)",
        states.get("FHIR (read + ingest)") == "LIVE"
        and states.get("ΗΔΙΚΑ / e-prescription") == "no_public_api")


if __name__ == "__main__":
    print("🔌 RAN-TEVU FHIR BRIDGE:")
    test_mapping_and_ingest()
    test_endpoints()
    print()
    if fails:
        print(f"❌ {len(fails)} ΑΠΟΤΥΧΙΕΣ: {fails}")
        sys.exit(1)
    print("✅ RAN-TEVU FHIR BRIDGE: mapping + ingest + endpoints + honest bridges-status.")
