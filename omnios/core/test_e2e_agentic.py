"""
test_e2e_agentic.py — ΟΛΟΚΛΗΡΟ το agentic stack μαζί, end-to-end, σε ΕΝΑ ιατρείο, ΕΝΑ bus.
================================================================================
Αποδεικνύει ότι όλα τα κομμάτια που χτίστηκαν συνδέονται & επικοινωνούν:
  reactive journey (bridge) → autonomous loop (learn on new outcomes) → autonomous dispatch
  (agents) → FHIR export — πάνω στο ΙΔΙΟ store, ΕΝΑ event bus, με audit intact σε όλα.
Τρέχει: python core/test_e2e_agentic.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clinic_store import ClinicStore
from clinic_autopilot import ClinicAutopilot
from autonomy_policy import AutonomyPolicy
from clinic_bridge import ClinicEventBus, ClinicBridge
from autonomous_agents import AutonomyOrchestrator
from autonomous_loop import AutonomousLoop
from fhir_bridge import patient_to_fhir, appointment_to_fhir, bundle, fhir_to_appointment
from audit import AUDIT
from datetime import datetime, timedelta

fails = []
def chk(label, ok):
    print(f"  {'✅' if ok else '❌ ΣΚΑΣΕ'} {label}")
    if not ok:
        fails.append(label)


def _past(d):
    return (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")


def test_full_agentic_stack_e2e():
    # ── Στήσιμο: ΕΝΑ ιατρείο, ΕΝΑ bus (reactive + proactive μοιράζονται nervous system) ──
    cs = ClinicStore(":memory:", tenant="e2e")
    cs.setup_clinic(name="Ιατρείο E2E", specialty="Φυσικοθεραπεία", default_value=40.0)
    p = cs.add_patient("Ασθενής E2E", phone="6970000001", amka="01018012345", consent=True)
    ap = ClinicAutopilot(cs)
    AutonomyPolicy(cs).set(enabled=True,
                           rules={"no_show_recovery": {"auto": True, "max_eur": 200},
                                  "reminder": {"auto": True}}, set_by="dr")
    bus = ClinicEventBus()
    bridge = ClinicBridge(cs, bus=bus)                    # reactive agents
    orch = AutonomyOrchestrator(cs, ap, bus=bus)         # proactive agents — ΙΔΙΟ bus
    loop = AutonomousLoop(cs, autopilot=ap, bus=bus)     # learning loop — ΙΔΙΟ bus

    # ── 1) INGEST εξωτερικού FHIR ραντεβού → πραγματικό booking (γέφυρα διαλειτουργικότητας) ──
    incoming = {"resourceType": "Appointment", "status": "booked", "start": f"{_past(-1)}T10:00:00",
                "description": "Συνεδρία", "participant": [{"actor": {"display": "Ασθενής E2E"}}]}
    f = fhir_to_appointment(incoming)
    a_ingested = cs.add_appointment(f["time"], f["patient"], f["service"], f["date"], 40.0, "confirmed", p["id"])
    chk("FHIR ingest → πραγματικό ραντεβού στο store", a_ingested is not None)

    # ── 2) REACTIVE journey: ένας πλήρης κύκλος ασθενή μέσα από τους 5 υπαλλήλους ──
    bridge.request_booking("Ασθενής E2E", "11:00", _past(-2), value=40.0, patient_id=p["id"])
    booked = next((e for e in bus.journey() if e["type"] == "appointment.booked"), None)
    aid = booked["payload"]["appointment_id"]
    bridge.arrive(aid, "Ασθενής E2E", patient_id=p["id"])
    bridge.complete_visit(aid, "Ασθενής E2E", transcript="Οσφυαλγία 5 ημερών.",
                          clinician_id="dr-001", value=40.0, patient_id=p["id"])
    chk("Reactive journey: invoice εκδόθηκε από τη ροή",
        any(e["type"] == "invoice.issued" for e in bus.journey()))

    # ── 3) Δημιούργησε ΝΕΑ outcomes (no-shows) → ο LEARNING loop τα μαθαίνει ──
    for k in range(6):
        na = cs.add_appointment(f"{9+k}:00", "Ασθενής E2E", "Συνεδρία", _past(k+1), 40.0, "confirmed", p["id"])
        cs.set_status(na["id"], "no_show")
    r = loop.cycle()
    chk("Learning loop: παρατήρησε νέα outcomes & έμαθε (rate ανέβηκε από τον prior)",
        r["observed_new"] >= 6 and r["learned_no_show_after"] > 0.23)

    # ── 4) PROACTIVE autonomous dispatch: οι agents ανέλαβαν δουλειά μόνοι τους (ΙΔΙΟ bus) ──
    chk("Proactive: autonomous agents εκτέλεσαν εξουσιοδοτημένη δουλειά (dispatched≥1)",
        r["dispatched"] >= 1)
    types = {e["type"] for e in bus.journey()}
    chk("ΕΝΑ bus κουβαλά reactive (appointment.booked) + proactive (work.*) — ενωμένο nervous system",
        "appointment.booked" in types and any(t.startswith("work.") for t in types))

    # ── 5) FHIR export: το επεισόδιο εξάγεται σε FHIR για κάθε EHR ──
    fp = patient_to_fhir(cs.list_patients()[0])
    b = bundle([fp] + [appointment_to_fhir(a) for a in cs.list_appointments()])
    chk("FHIR export: Patient + Appointments σε έγκυρο Bundle",
        b["resourceType"] == "Bundle" and any(e["resource"]["resourceType"] == "Patient" for e in b["entry"]))

    # ── 6) Το θεμέλιο: audit intact μετά από ΟΛΑ (reactive + proactive + learning) ──
    chk("Audit chain intact μετά ΟΛΟ το e2e stack", AUDIT.verify())
    bridge_events = sum(1 for e in bus.journey() if e["type"].startswith(("appointment", "work", "invoice", "checkout")))
    chk("Πλήρης ιχνηλασιμότητα: reactive + proactive events στο ίδιο journey", bridge_events >= 5)


if __name__ == "__main__":
    print("🔗 E2E AGENTIC STACK — reactive + proactive + learning + FHIR, ΕΝΑ ιατρείο, ΕΝΑ bus:")
    test_full_agentic_stack_e2e()
    print()
    if fails:
        print(f"❌ {len(fails)} ΑΠΟΤΥΧΙΕΣ: {fails}")
        sys.exit(1)
    print("✅ E2E: FHIR-ingest → reactive journey → learning-on-new-data → autonomous-dispatch →")
    print("   FHIR-export, ΟΛΑ συνδεδεμένα σε ΕΝΑ bus, audit intact. Το πλήρες agentic σύστημα δουλεύει.")
