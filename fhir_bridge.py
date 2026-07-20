"""
fhir_bridge.py — Ran-Tevu ως FHIR R4 endpoint (γέφυρα διαλειτουργικότητας, per-slug).
================================================================================
Χαρτογραφεί bookings / bo_patients / bo_invoices ενός ιατρείου (slug) σε FHIR R4 resources, ώστε
οποιοδήποτε σύστημα μιλάει FHIR (EHR, booking platform με FHIR, EHDS-συμβατή υποδομή) να διαβάζει τα
ραντεβού/ασθενείς ή να γράφει ραντεβού — χωρίς custom κόλλα.

ΤΙΜΙΟ ΟΡΙΟ (ίδιο με OmniOS): δουλεύει ΜΟΝΟ με συστήματα που ΗΔΗ μιλάνε FHIR. ΗΔΙΚΑ/e-prescription/
Doctoranytime ΔΕΝ εκθέτουν FHIR ούτε κανένα δημόσιο API → δεν συνδέονται από εδώ. Deterministic, stdlib.
Tests: python tests/test_fhir_bridge.py
"""
from __future__ import annotations

from typing import Any

FHIR_BASE = "urn:rantevu:fhir"
_APPT_STATUS = {"confirmed": "booked", "pending_outcome": "arrived", "completed": "fulfilled",
                "no_show": "noshow", "cancelled": "cancelled"}


def patient_to_fhir(p: dict) -> dict:
    """bo_patients row → FHIR R4 Patient. phone = natural id· ΑΜΚΑ = εθνικό identifier."""
    ident = [{"system": f"{FHIR_BASE}/phone-id", "value": p.get("phone", "")}]
    if p.get("amka"):
        ident.append({"system": "https://www.amka.gr", "value": p["amka"]})
    res: dict[str, Any] = {"resourceType": "Patient", "id": str(p.get("phone", "")),
                           "identifier": ident, "active": p.get("status", "active") == "active"}
    if p.get("name"):
        res["name"] = [{"text": p["name"]}]
    tel = []
    if p.get("phone"):
        tel.append({"system": "phone", "value": p["phone"], "use": "mobile"})
    if p.get("email"):
        tel.append({"system": "email", "value": p["email"]})
    if tel:
        res["telecom"] = tel
    if p.get("dob"):
        res["birthDate"] = p["dob"]
    return res


def booking_to_fhir(b: dict) -> dict:
    """bookings row → FHIR R4 Appointment. start = ISO date+time."""
    return {"resourceType": "Appointment", "id": str(b["id"]),
            "status": _APPT_STATUS.get(b.get("status", "confirmed"), "booked"),
            "start": f"{b['date']}T{b['time']}:00",
            "minutesDuration": int(b.get("duration", 30) or 30),
            "description": b.get("service", ""),
            "participant": [{"actor": {"reference": f"Patient/{b.get('phone', '')}",
                                       "display": b.get("name", "")},
                             "status": "accepted"}]}


def invoice_to_fhir(inv: dict) -> dict:
    """bo_invoices row → FHIR R4 Invoice (totalGross = total με ΦΠΑ)."""
    return {"resourceType": "Invoice", "id": str(inv["id"]),
            "status": "balanced" if inv.get("status") == "paid" else "issued",
            "subject": {"reference": f"Patient/{inv.get('patient_phone', '')}",
                        "display": inv.get("patient_name", "")},
            "totalGross": {"value": float(inv.get("total", inv.get("amount", 0)) or 0), "currency": "EUR"},
            "extension": [{"url": f"{FHIR_BASE}/mydata-status", "valueString": inv.get("mydata_status", "pending")}]}


def bundle(resources: list[dict], bundle_type: str = "searchset") -> dict:
    return {"resourceType": "Bundle", "type": bundle_type, "total": len(resources),
            "entry": [{"resource": r} for r in resources]}


def fhir_to_booking(fhir: dict) -> dict:
    """Εισερχόμενο FHIR Appointment → internal πεδία για db.create_booking. Raises ValueError αν άκυρο."""
    if (fhir or {}).get("resourceType") != "Appointment":
        raise ValueError("not_a_fhir_appointment")
    start = fhir.get("start", "")
    if "T" not in start:
        raise ValueError("appointment.start must be ISO datetime (YYYY-MM-DDThh:mm)")
    date, _, tm = start.partition("T")
    tm = tm[:5]
    name, phone = "", ""
    for pt in (fhir.get("participant") or []):
        actor = pt.get("actor", {})
        name = actor.get("display", "") or name
        ref = actor.get("reference", "")
        if ref.startswith("Patient/"):
            phone = ref.split("/", 1)[1]
    if not (date and tm and name):
        raise ValueError("missing date/time/participant")
    return {"date": date, "time": tm, "name": name, "phone": phone,
            "service": fhir.get("description", "Ραντεβού"),
            "duration": int(fhir.get("minutesDuration", 30) or 30)}


def capability_statement() -> dict:
    return {"resourceType": "CapabilityStatement", "status": "active", "fhirVersion": "4.0.1",
            "format": ["json"], "software": {"name": "Ran-Tevu FHIR bridge"},
            "rest": [{"mode": "server", "resource": [
                {"type": "Patient", "interaction": [{"code": "search-type"}]},
                {"type": "Appointment", "interaction": [{"code": "search-type"}, {"code": "create"}]},
                {"type": "Invoice", "interaction": [{"code": "search-type"}]},
            ]}]}
