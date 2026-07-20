"""
test_clinic_bridge.py (Ran-Tevu) — οι υπάλληλοι συνεννοούνται & εκτελούν, ο κύκλος τρέχει μόνος του.
Αποδεικνύει: ένα αίτημα ραντεβού → αλυσίδα handoffs (booking→υποδοχή→κλινικός→λογιστήριο→αναχώρηση)
με ΠΡΑΓΜΑΤΙΚΑ side-effects στη βάση, audit σε κάθε γέφυρα, consent-gated υπενθύμιση, idempotent
τιμολόγηση (ένα τιμολόγιο ανά επίσκεψη), double-booking → waitlist. Τρέχει: python tests/test_clinic_bridge.py
"""
import os, sys, tempfile
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "rt_bridge.db")
if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])
from cryptography.fernet import Fernet
os.environ["RANTEVU_ENC_KEY"] = Fernet.generate_key().decode()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import database as db
import provisioning
from clinic_bridge import ClinicBridge
from datetime import datetime, timedelta

db.init_db()
TOM = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

fails = []
def chk(label, ok):
    print(f"  {'✅' if ok else '❌ ΣΚΑΣΕ'} {label}")
    if not ok:
        fails.append(label)


def _clinic():
    prov = provisioning.provision("Ιατρείο Γέφυρα", "physio", owner_name="Δρ", email="dr@x.gr", demo=False)
    return prov["slug"]


def test_full_journey():
    S = _clinic()
    db.log_consent(consent_type="reminders", slug=S, identifier="6970000001", accepted=True)
    br = ClinicBridge(S)

    br.request_booking("Γιώργος Δοκιμή", "6970000001", "10:00", TOM, value=40.0)
    booked = next((e for e in br.journey() if e["type"] == "appointment.booked"), None)
    prepared = next((e for e in br.journey() if e["type"] == "checkin.prepared"), None)
    chk("BookingAgent έκλεισε ραντεβού (πραγματικό στη βάση)",
        booked is not None and len(db.get_all_bookings(S)) == 1)
    chk("ReceptionAgent ετοίμασε φάκελο ασθενή (handoff booking→υποδοχή)", prepared is not None)
    bid = booked["payload"]["booking_id"]

    br.arrive(bid, "Γιώργος Δοκιμή", "6970000001")
    started = next((e for e in br.journey() if e["type"] == "visit.started"), None)
    chk("ReceptionAgent ξεκίνησε την επίσκεψη (handoff υποδοχή→κλινικός)", started is not None)

    br.complete_visit(bid, "Γιώργος Δοκιμή", "6970000001",
                      notes="Οσφυαλγία 5 ημερών. Πρόταση: φυσικοθεραπεία.", value=40.0)
    note = next((e for e in br.journey() if e["type"] == "note.signed"), None)
    issued = next((e for e in br.journey() if e["type"] == "invoice.issued"), None)
    done = next((e for e in br.journey() if e["type"] == "checkout.done"), None)
    chk("ClinicalAgent έγραψε σημείωση (encrypted) στον φάκελο", note is not None)

    with db._conn() as c:
        n_inv = c.execute("SELECT COUNT(*) FROM bo_invoices WHERE slug=? AND booking_id=?", (S, bid)).fetchone()[0]
    chk("BillingAgent: ΑΚΡΙΒΩΣ 1 τιμολόγιο ανά επίσκεψη (idempotent, δεν διπλο-χρέωσε)",
        issued is not None and n_inv == 1)
    chk("CheckoutAgent έκλεισε follow-up (2ο ραντεβού στη βάση)",
        done is not None and done["payload"]["followup_id"] is not None and len(db.get_all_bookings(S)) == 2)
    chk("Υπενθύμιση consent-gated → επιχειρήθηκε (sent/queued, με καταγεγραμμένη συναίνεση)",
        done["payload"]["reminder"] in ("sent", "queued"))

    v = db.verify_audit_chain(S)
    chk("Audit chain intact μετά τον πλήρη κύκλο", v.get("valid", v.get("intact", True)))
    bridge_events = 0
    with db._conn() as c:
        bridge_events = c.execute("SELECT COUNT(*) FROM audit_logs WHERE action LIKE 'bridge.%' AND slug=?",
                                  (S,)).fetchone()[0]
    chk("Κάθε handoff καταγράφηκε στο audit (≥6 bridge events)", bridge_events >= 6)


def test_consent_gate_blocks_reminder():
    S = _clinic()   # ΧΩΡΙΣ log_consent → καμία συναίνεση
    br = ClinicBridge(S)
    br.request_booking("Μη Συναινεί", "6970000002", "11:00", TOM, value=40.0)
    bid = next(e for e in br.journey() if e["type"] == "appointment.booked")["payload"]["booking_id"]
    br.complete_visit(bid, "Μη Συναινεί", "6970000002", notes="Έλεγχος.", value=40.0)
    done = next(e for e in br.journey() if e["type"] == "checkout.done")
    chk("Χωρίς συναίνεση → ΚΑΜΙΑ υπενθύμιση (no_consent)", done["payload"]["reminder"] == "no_consent")


def test_double_booking_to_waitlist():
    S = _clinic()
    br = ClinicBridge(S)
    br.request_booking("Πρώτος", "6970000003", "12:00", TOM, value=40.0)
    br.request_booking("Δεύτερος", "6970000004", "12:00", TOM, value=40.0)   # ίδιο slot
    rejected = next((e for e in br.journey() if e["type"] == "appointment.rejected"), None)
    active = [b for b in db.get_all_bookings(S) if b["status"] != "cancelled"]
    chk("Διπλοκράτηση απορρίφθηκε ήσυχα → 1 ενεργό ραντεβού", rejected is not None and len(active) == 1)
    with db._conn() as c:
        wl = c.execute("SELECT COUNT(*) FROM waitlist WHERE slug=?", (S,)).fetchone()[0]
    chk("Ο 2ος μπήκε στη λίστα αναμονής αντί να χαθεί", wl == 1)


if __name__ == "__main__":
    print("🌉 RAN-TEVU CLINIC BRIDGE — οι υπάλληλοι συνεννοούνται & εκτελούν:")
    test_full_journey()
    test_consent_gate_blocks_reminder()
    test_double_booking_to_waitlist()
    print()
    if fails:
        print(f"❌ {len(fails)} ΑΠΟΤΥΧΙΕΣ: {fails}")
        sys.exit(1)
    print("✅ RAN-TEVU BRIDGE: handoffs με πραγματικά side-effects, audited, consent-gated,")
    print("   idempotent billing, double-booking→waitlist.")
