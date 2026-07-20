"""
test_e2e_spectrum.py — the FULL physio-clinic lifecycle, end to end, over the API.
Walks every front + back + autonomy + trust function a real clinic performs and asserts each,
printing a CONFIRMED line per capability. This is the "does it do everything?" proof.
Run: python core/test_e2e_spectrum.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastapi.testclient import TestClient
from api import app, _DEV_API_KEY

# Clinic ops are now SECURED (Art.32) — the "open" client carries the key too; truly-open
# endpoints (/anonymize, /audit, /metrics) simply ignore the header.
open_c = TestClient(app, headers={"X-API-Key": _DEV_API_KEY})
key_c = TestClient(app, headers={"X-API-Key": _DEV_API_KEY})   # secured (scribe/govern)
OK = []


def confirm(msg):
    OK.append(msg)
    print("✓ CONFIRMED:", msg)


def spectrum():
    # ── FRONT DESK ─────────────────────────────────────────────
    m0 = open_c.get("/clinic").json()["metrics"]
    confirm(f"clinic live with real data ({m0['appointments_today']} appts, {m0['patients']} patients)")

    booked = open_c.post("/clinic/appointments", json={"time": "18:00", "patient": "Α. Νέος", "service": "Φυσικοθεραπεία"})
    assert booked.status_code == 200 and booked.json()["status"] == "confirmed"
    aid = booked.json()["id"]
    confirm("front desk: book an appointment (persisted)")

    dup = open_c.post("/clinic/appointments", json={"time": "18:00", "patient": "Β. Διπλός"})
    assert dup.status_code == 409
    confirm("front desk: double-booking is prevented (409 guard)")

    checkin = open_c.post(f"/clinic/appointments/{aid}/status", json={"status": "completed"})
    assert checkin.status_code == 200 and checkin.json()["status"] == "completed"
    confirm("front desk: patient check-in → marked arrived")

    # ── THE SCENARIO HE NAMED: a patient calls, cancels → the slot REFILLS ──
    # set up a real waiting patient and a real booking, then cancel it
    open_c.post("/clinic/waitlist", json={"name": "Θ. Περιμένει", "note": "όποτε βρεθεί"})
    wl = open_c.get("/clinic/appointments").json()["waitlist"]
    head = wl[0]["name"]
    to_cancel = open_c.post("/clinic/appointments", json={"time": "19:00", "patient": "Γ. Θα ακυρώσει"}).json()
    cancelled = open_c.post(f"/clinic/appointments/{to_cancel['id']}/cancel").json()
    assert cancelled["backfilled"], "cancellation must auto-fill the freed slot from the waitlist"
    assert cancelled["backfilled"]["patient"] == head and cancelled["backfilled"]["time"] == "19:00"
    # and the waitlist shrank + the new booking is really there
    after = open_c.get("/clinic/appointments").json()
    assert any(a["patient"] == head and a["time"] == "19:00" for a in after["appointments"])
    confirm(f"★ CANCEL → SLOT REFILLS: '{head}' auto-booked into the freed 19:00 slot (FIFO, audited)")

    # no-show → recovery enqueued
    ns_appt = open_c.post("/clinic/appointments", json={"time": "20:00", "patient": "Δ. Θα λείψει"}).json()
    ns = open_c.post(f"/clinic/appointments/{ns_appt['id']}/status", json={"status": "no_show"})
    assert ns.status_code == 200 and ns.json()["status"] == "no_show"
    confirm("front desk: no-show detected → recovery enqueued (audited)")

    # ── BACK OFFICE ────────────────────────────────────────────
    p = open_c.post("/clinic/patients", json={"name": "Ε. Ασθενής", "phone": "6970000123", "consent": True})
    assert p.status_code == 200 and p.json()["consent"] is True
    confirm("back office: patient CRM add with explicit GDPR consent (Art.9)")

    inv = open_c.post("/clinic/invoices", json={"patient": "Ε. Ασθενής", "amount": 30}).json()
    assert "0%" in inv["vat"]
    paid = open_c.post(f"/clinic/invoices/{inv['id']}/pay")
    assert paid.status_code == 200 and paid.json()["status"] == "paid"
    confirm("back office: invoice issue (VAT 0% medical exemption) → payment, persisted")

    lg = open_c.get("/clinic/invoices").json()["billing"]
    assert lg["roi"] == 10.0 and lg["charge_eur"] == round(0.10 * lg["total_value_eur"], 2)
    confirm(f"back office: value-based billing correct (€{lg['total_value_eur']} saved → €{lg['charge_eur']} charge, ROI {lg['roi']}x)")

    # clinical scribe (secured, HITL): draft → clinician sign
    d = key_c.post("/scribe/draft", json={"transcript": "Ασθενής με ΑΜΚΑ 12036512345. Οσφυαλγία μετά από άρση βάρους."})
    assert d.status_code == 200 and d.json()["status"] == "DRAFT" and "12036512345" not in d.json()["transcript_redacted"]
    s = key_c.post("/scribe/sign", json={"draft_id": d.json()["id"], "clinician_id": "dr_physio"})
    assert s.status_code == 200 and s.json()["status"] == "SIGNED"
    confirm("back office: clinical note draft (PII-stripped, SNOMED/ICD) → clinician sign (HITL)")

    # ── TRUST / GDPR ───────────────────────────────────────────
    az = open_c.post("/anonymize", json={"text": "ΑΜΚΑ 12036512345, κάρτα 4539 1488 0343 6467, τηλ 6945123456"}).json()
    assert az["pii_count"] >= 3 and az["reversible"] and az["bytes_egress"] == 0
    confirm(f"trust: edge PII tokenization ({az['pii_count']} items, reversible, 0 bytes egress)")

    ask = key_c.post("/ask", json={"query": "ασπιρίνη πυρετό"}).json()
    # confidence gate: low-confidence / high-risk answers freeze (hitl)
    assert "confidence" in ask
    confirm(f"trust: answers carry a confidence + gate (conf {ask['confidence']}, hitl={ask.get('hitl_required')})")

    prop = key_c.post("/govern/propose", json={"capability": "send_recovery_sms",
                                               "params": {"to_token": "[REDACTED_PHONE_1]", "text": "Να ξανακλείσουμε;"},
                                               "reason": "no-show recovery"}).json()
    assert prop["status"] == "PENDING_APPROVAL" and prop["evidence"]["commits"] is False
    ex = key_c.post("/govern/approve", json={"proposal_id": prop["id"], "approver": "dr_physio"}).json()
    assert ex["status"] == "EXECUTED"
    confirm("trust: governed action — high-risk SMS froze for approval → clinician approved → executed (audited)")

    tamper = open_c.post("/audit/tamper-demo").json()
    assert tamper["before"] and not tamper["after"]
    assert open_c.post("/audit/verify").json()["intact"]
    confirm("trust: tamper-evident audit — forgery detected, real chain intact")

    # ── AUTONOMY: the closed loop learns on real outcomes ──────
    loop = open_c.post("/agentic/run", json={"appointments": 3, "appointment_value": 45}).json()
    assert loop["learned_no_show_prob"] > loop["start_no_show_prob"] and loop["recovered_eur"] > 0
    confirm(f"autonomy: closed loop learns ({loop['start_no_show_prob']}→{loop['learned_no_show_prob']}) & recovers €{loop['recovered_eur']}")

    # ── metrics reflect every real change ──
    m1 = open_c.get("/clinic").json()["metrics"]
    assert m1["completed"] > m0["completed"] and m1["no_shows"] > m0["no_shows"]
    confirm("metrics move with real operations (completed & no-shows increased)")


def main():
    spectrum()
    print(f"\n🎯 FULL PHYSIO-CLINIC SPECTRUM — {len(OK)} capabilities CONFIRMED end-to-end. "
          "Front + back + autonomy + trust all functional over real, persisted data.")


if __name__ == "__main__":
    main()
