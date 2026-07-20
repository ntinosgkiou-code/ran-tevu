"""
test_clinic_bridge.py — οι υπάλληλοι συνεννοούνται & εκτελούν, ο κύκλος τρέχει από μόνος του.
Αποδεικνύει: ένα ΜΟΝΟ αίτημα ραντεβού πυροδοτεί μια αλυσίδα handoffs (booking→υποδοχή→κλινικός→
λογιστήριο→αναχώρηση) όπου ΚΑΘΕ υπάλληλος κάνει ΠΡΑΓΜΑΤΙΚΟ side-effect στο κοινό store, κάθε
γέφυρα καταγράφεται στο audit, το consent gate μπλοκάρει outreach χωρίς συναίνεση, και το επεισόδιο
εξάγεται ως FHIR R4 Bundle χωρίς PII. Τρέχει: python core/test_clinic_bridge.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clinic_store import ClinicStore
from clinic_bridge import ClinicBridge, journey_to_fhir
from audit import AUDIT

fails = []
def chk(label, ok):
    print(f"  {'✅' if ok else '❌ ΣΚΑΣΕ'} {label}")
    if not ok:
        fails.append(label)


def test_full_journey_runs_itself():
    cs = ClinicStore(":memory:", tenant="bridge-clinic")
    cs.setup_clinic(name="Ιατρείο Γέφυρα", specialty="Φυσικοθεραπεία", default_value=40.0)
    p = cs.add_patient("Γιώργος Δοκιμή", phone="6970000001", consent=True)
    br = ClinicBridge(cs, clinic_afm="")   # χωρίς AFM → myDATA δεν επιχειρείται (καθαρό)

    # 1) ΕΝΑ αίτημα ραντεβού → ο BookingAgent κλείνει → ο ReceptionAgent ετοιμάζει φάκελο
    br.request_booking("Γιώργος Δοκιμή", "10:00", "2026-08-01", value=40.0, patient_id=p["id"])
    booked = next((e for e in br.journey() if e["type"] == "appointment.booked"), None)
    prepared = next((e for e in br.journey() if e["type"] == "checkin.prepared"), None)
    chk("BookingAgent έκλεισε ραντεβού (πραγματικό appointment στο store)",
        booked is not None and len(cs.list_appointments()) == 1)
    chk("ReceptionAgent αντέδρασε στο event & ετοίμασε check-in (handoff booking→υποδοχή)",
        prepared is not None)
    aid = booked["payload"]["appointment_id"]

    # 2) ο ασθενής φτάνει → visit ξεκινά → ο γιατρός ολοκληρώνει με transcript
    br.arrive(aid, "Γιώργος Δοκιμή", patient_id=p["id"])
    started = next((e for e in br.journey() if e["type"] == "visit.started"), None)
    chk("ReceptionAgent ξεκίνησε την επίσκεψη στην άφιξη (handoff υποδοχή→κλινικός)", started is not None)

    br.complete_visit(aid, "Γιώργος Δοκιμή",
                      transcript="Ο ασθενής αναφέρει οσφυαλγία 5 ημερών. Πρόταση: φυσικοθεραπεία.",
                      clinician_id="dr-lic-123", value=40.0, patient_id=p["id"])

    # 3) ClinicalAgent: SOAP υπογεγραμμένο στον φάκελο → visit.billable
    note_signed = next((e for e in br.journey() if e["type"] == "note.signed"), None)
    chk("ClinicalAgent δόμησε & υπέγραψε SOAP στον φάκελο (πραγματική note)",
        note_signed is not None and len(cs.list_notes()) == 1)

    # 4) BillingAgent: τιμολόγιο εκδόθηκε (πραγματικό invoice)
    issued = next((e for e in br.journey() if e["type"] == "invoice.issued"), None)
    chk("BillingAgent εξέδωσε τιμολόγιο αυτόματα (handoff κλινικός→λογιστήριο)",
        issued is not None and len(cs.list_invoices()) == 1)

    # 5) CheckoutAgent: follow-up + υπενθύμιση (consent=True → πάει στην ουρά channels)
    done = next((e for e in br.journey() if e["type"] == "checkout.done"), None)
    chk("CheckoutAgent έκλεισε follow-up + έστειλε υπενθύμιση (handoff λογιστήριο→αναχώρηση)",
        done is not None and done["payload"]["followup_id"] is not None)
    chk("Follow-up = πραγματικό 2ο ραντεβού στο store", len(cs.list_appointments()) == 2)
    chk("Υπενθύμιση consent-gated → μπήκε στην durable ουρά (queued, όχι ψεύτικο send)",
        done["payload"]["reminder"] in ("queued", "sent"))

    # 6) Κάθε γέφυρα στο audit — η επικοινωνία ΕΙΝΑΙ το ίχνος
    bridge_events = [r for r in AUDIT.tail(200) if r["event"].startswith("bridge.")]
    chk("Κάθε handoff καταγράφηκε στο tamper-evident audit", len(bridge_events) >= 6)
    chk("Audit chain intact μετά τον πλήρη κύκλο", AUDIT.verify())

    return br


def test_consent_gate_blocks_outreach():
    cs = ClinicStore(":memory:", tenant="bridge-noconsent")
    cs.setup_clinic(name="Ιατρείο Β", specialty="Φυσικοθεραπεία", default_value=40.0)
    p = cs.add_patient("Μη Συναινεί", phone="6970000002", consent=False)   # ΔΕΝ συναινεί
    br = ClinicBridge(cs)
    br.request_booking("Μη Συναινεί", "11:00", "2026-08-02", value=40.0, patient_id=p["id"])
    aid = next(e for e in br.journey() if e["type"] == "appointment.booked")["payload"]["appointment_id"]
    br.arrive(aid, "Μη Συναινεί", patient_id=p["id"])
    br.complete_visit(aid, "Μη Συναινεί", transcript="Έλεγχος ρουτίνας.",
                      clinician_id="dr-lic-123", value=40.0, patient_id=p["id"])
    done = next(e for e in br.journey() if e["type"] == "checkout.done")
    chk("Χωρίς συναίνεση (Art.9) → ΚΑΜΙΑ υπενθύμιση στάλθηκε/μπήκε σε ουρά",
        done["payload"]["reminder"] == "no_consent")


def test_double_booking_rejected_gracefully():
    cs = ClinicStore(":memory:", tenant="bridge-dupe")
    cs.setup_clinic(name="Ιατρείο Γ", specialty="Φυσικοθεραπεία", default_value=40.0)
    p1 = cs.add_patient("Πρώτος", phone="6970000003", consent=True)
    p2 = cs.add_patient("Δεύτερος", phone="6970000004", consent=True)
    br = ClinicBridge(cs)
    br.request_booking("Πρώτος", "12:00", "2026-08-03", value=40.0, patient_id=p1["id"])
    br.request_booking("Δεύτερος", "12:00", "2026-08-03", value=40.0, patient_id=p2["id"])  # ίδιο slot
    rejected = next((e for e in br.journey() if e["type"] == "appointment.rejected"), None)
    active = [a for a in cs.list_appointments() if a["status"] != "cancelled"]
    chk("Διπλοκράτηση απορρίφθηκε ήσυχα (BookingAgent δεν έσκασε) → 1 ενεργό ραντεβού",
        rejected is not None and len(active) == 1)
    chk("Ο 2ος μπήκε στη λίστα αναμονής αντί να χαθεί", len(cs.list_waitlist()) == 1)


def test_journey_exports_fhir_zero_pii():
    br = test_full_journey_runs_itself()
    bundle = journey_to_fhir(br, patient_token="vault-tok-abc")
    chk("FHIR Bundle σωστού τύπου (R4 collection)",
        bundle.get("resourceType") == "Bundle" and bundle.get("type") == "collection")
    types = {e["resource"]["resourceType"] for e in bundle["entry"]}
    chk("Bundle περιέχει Patient + Encounter + Claim (η γέφυρα προς κάθε EHR)",
        {"Patient", "Encounter", "Claim"} <= types)
    blob = str(bundle)
    chk("ΜΗΔΕΝ PII στο Bundle (μόνο vault token, ποτέ όνομα/ΑΜΚΑ/τηλέφωνο)",
        "Γιώργος" not in blob and "6970000001" not in blob and "vault-tok-abc" in blob)


if __name__ == "__main__":
    print("🌉 CLINIC BRIDGE — οι υπάλληλοι συνεννοούνται & εκτελούν:")
    test_full_journey_runs_itself()
    test_consent_gate_blocks_outreach()
    test_double_booking_rejected_gracefully()
    test_journey_exports_fhir_zero_pii()
    print()
    if fails:
        print(f"❌ {len(fails)} ΑΠΟΤΥΧΙΕΣ: {fails}")
        sys.exit(1)
    print("✅ CLINIC BRIDGE: όλα τα handoffs εκτελούνται με πραγματικά side-effects, audited,")
    print("   consent-gated, double-booking-safe, εξαγώγιμα σε FHIR χωρίς PII.")
