"""
fhir_bridge.py — Το OmniOS ως FHIR R4 endpoint (η ΠΡΑΓΜΑΤΙΚΗ γέφυρα διαλειτουργικότητας).
================================================================================
Το `fhir_adapter.py` χαρτογραφεί ΕΝΑ claim → FHIR (για payer). Αυτό εδώ κάνει το ΙΑΤΡΕΙΟ ολόκληρο
προσβάσιμο ως FHIR: Patient / Appointment / Invoice ↔ FHIR R4 resources, + ένα CapabilityStatement.
Έτσι ΟΠΟΙΟΔΗΠΟΤΕ σύστημα μιλάει FHIR (EHR, booking platform που εκθέτει FHIR, EHDS-συμβατή υποδομή)
μπορεί να ΔΙΑΒΑΣΕΙ τα ραντεβού/ασθενείς ή να ΓΡΑΨΕΙ ένα ραντεβού — χωρίς custom κόλλα.

ΤΙΜΙΟ ΟΡΙΟ: αυτό δουλεύει ΜΟΝΟ με συστήματα που ΗΔΗ μιλάνε FHIR. Η ελληνική ΗΔΙΚΑ/e-prescription/
Doctoranytime ΔΕΝ εκθέτουν FHIR (ούτε κανένα δημόσιο API) → δεν συνδέονται από εδώ. Η γέφυρα υπάρχει
και είναι έτοιμη· η κρατική άκρη δεν μιλάει ακόμη τη γλώσσα (EHDS: ορίζοντας 2027-2029).

Τα read endpoints είναι tenant-scoped (CTX) → το ιατρείο διαβάζει ΤΑ ΔΙΚΑ ΤΟΥ δεδομένα (ίδιος data
controller, GDPR-νόμιμο)· PII δεν διαρρέει ΠΟΤΕ εκτός του trust boundary του ιατρείου. Deterministic,
stdlib. Tests: python core/test_fhir_bridge.py
"""
from __future__ import annotations

from typing import Any, Optional

FHIR_BASE = "urn:omnios:fhir"
_APPT_STATUS = {"confirmed": "booked", "arrived": "arrived", "completed": "fulfilled",
                "no_show": "noshow", "cancelled": "cancelled"}


# ─────────────────────────── resource → FHIR ────────────────────────────────
def patient_to_fhir(p: dict) -> dict:
    """Clinic patient → FHIR R4 Patient. Το ΑΜΚΑ μπαίνει ως εθνικό identifier (AMKA system)."""
    ident = [{"system": f"{FHIR_BASE}/patient-id", "value": p["id"]}]
    if p.get("amka"):
        ident.append({"system": "https://www.amka.gr", "value": p["amka"]})
    res: dict[str, Any] = {"resourceType": "Patient", "id": p["id"], "identifier": ident,
                           "active": p.get("tag") != "ανενεργός"}
    if p.get("name"):
        res["name"] = [{"text": p["name"]}]
    tel = []
    if p.get("phone"):
        tel.append({"system": "phone", "value": p["phone"], "use": "mobile"})
    if p.get("email"):
        tel.append({"system": "email", "value": p["email"]})
    if tel:
        res["telecom"] = tel
    # συναίνεση επικοινωνίας ως extension (μηχανικά αναγνώσιμη από το άλλο σύστημα)
    res["extension"] = [{"url": f"{FHIR_BASE}/communication-consent",
                         "valueBoolean": bool(p.get("consent", False))}]
    return res


def appointment_to_fhir(a: dict) -> dict:
    """Clinic appointment → FHIR R4 Appointment. start = ISO date+time."""
    start = f"{a['date']}T{a['time']}:00"
    return {"resourceType": "Appointment", "id": a["id"],
            "status": _APPT_STATUS.get(a.get("status", "confirmed"), "booked"),
            "start": start,
            "description": a.get("service", ""),
            "participant": [{"actor": {"reference": f"Patient/{a.get('patient_id') or a['patient']}",
                                       "display": a["patient"]},
                             "status": "accepted"}],
            "extension": [{"url": f"{FHIR_BASE}/session-value-eur",
                           "valueDecimal": float(a.get("value", 0) or 0)}]}


def invoice_to_fhir(inv: dict) -> dict:
    """Clinic invoice → FHIR R4 Invoice."""
    return {"resourceType": "Invoice", "id": inv["id"],
            "status": "balanced" if inv.get("status") == "paid" else "issued",
            "subject": {"reference": f"Patient/{inv.get('patient_id') or inv.get('patient')}",
                        "display": inv.get("patient", "")},
            "totalGross": {"value": float(inv.get("amount", 0)), "currency": "EUR"}}


def bundle(resources: list[dict], bundle_type: str = "searchset") -> dict:
    return {"resourceType": "Bundle", "type": bundle_type, "total": len(resources),
            "entry": [{"resource": r} for r in resources]}


# ─────────────────────────── FHIR → internal (ingest) ───────────────────────
def fhir_to_appointment(fhir: dict) -> dict:
    """Εισερχόμενο FHIR Appointment (από σύστημα που μιλάει FHIR) → internal πεδία για add_appointment.
    Δέχεται `start` ISO (YYYY-MM-DDThh:mm[:ss]). Raises ValueError αν λείπει/είναι άκυρο."""
    if (fhir or {}).get("resourceType") != "Appointment":
        raise ValueError("not_a_fhir_appointment")
    start = fhir.get("start", "")
    if "T" not in start:
        raise ValueError("appointment.start must be ISO datetime (YYYY-MM-DDThh:mm)")
    date, _, tm = start.partition("T")
    tm = tm[:5]                                   # hh:mm
    parts = fhir.get("participant") or []
    patient = ""
    for pt in parts:
        actor = pt.get("actor", {})
        patient = actor.get("display") or actor.get("reference", "").split("/")[-1]
        if patient:
            break
    if not (date and tm and patient):
        raise ValueError("missing date/time/participant")
    return {"date": date, "time": tm, "patient": patient,
            "service": fhir.get("description", "")}


# ─────────────────────────── CapabilityStatement ────────────────────────────
def capability_statement() -> dict:
    """FHIR standard discovery (GET /fhir/metadata) — τι υποστηρίζει αυτός ο FHIR server."""
    return {"resourceType": "CapabilityStatement", "status": "active", "fhirVersion": "4.0.1",
            "format": ["json"],
            "software": {"name": "OmniOS FHIR bridge"},
            "rest": [{"mode": "server", "resource": [
                {"type": "Patient", "interaction": [{"code": "search-type"}]},
                {"type": "Appointment", "interaction": [{"code": "search-type"}, {"code": "create"}]},
                {"type": "Invoice", "interaction": [{"code": "search-type"}]},
            ]}]}
