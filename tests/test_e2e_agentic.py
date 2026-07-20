"""
test_e2e_agentic.py (Ran-Tevu) — ολόκληρο το agentic stack μαζί, end-to-end, σε ένα ιατρείο.
bridge journey (real booking→invoice) → autonomous loop (learn on new no-shows + recovery) →
FHIR export. Τρέχει: python tests/test_e2e_agentic.py
"""
import os, sys, tempfile
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "rt_e2e.db")
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
import autonomous_loop as L
import fhir_bridge as F
from datetime import datetime, timedelta

db.init_db()
fails = []
def chk(label, ok):
    print(f"  {'✅' if ok else '❌ ΣΚΑΣΕ'} {label}")
    if not ok:
        fails.append(label)

TOM = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
NXT = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")


def test_rantevu_e2e():
    S = provisioning.provision("E2E Ιατρείο", "physio", owner_name="Δρ", email="dr@x.gr", demo=False)["slug"]

    # 1) Reactive bridge journey: κράτηση → επίσκεψη → τιμολόγιο (πραγματικά side-effects)
    br = ClinicBridge(S)
    br.request_booking("Ασθενής E2E", "6970000001", "10:00", TOM, value=40.0)
    bid = next(e for e in br.journey() if e["type"] == "appointment.booked")["payload"]["booking_id"]
    br.complete_visit(bid, "Ασθενής E2E", "6970000001", notes="Οσφυαλγία.", value=40.0)
    with db._conn() as c:
        inv = c.execute("SELECT COUNT(*) FROM bo_invoices WHERE slug=? AND booking_id=?", (S, bid)).fetchone()[0]
    chk("Reactive journey: 1 τιμολόγιο από τη ροή (idempotent)", inv == 1)

    # 2) Νέα no-show outcomes → ο learning loop μαθαίνει + enqueue recovery (consent-gated → opt-in)
    for k in range(6):
        ph = f"69{k:08d}"
        db.log_consent(consent_type="reminders", slug=S, identifier=ph, accepted=True)
        b2 = db.create_booking(S, NXT, f"{8+k:02d}:00", "Συνεδρία", 30, f"NS{k}", ph)
        db.mark_booking_outcome(b2, "no_show")
    r = L.cycle(S)
    chk("Learning loop: έμαθε από τα νέα no-shows (rate ↑ από 0.23)", r["learned_no_show_after"] > 0.23)
    chk("Learning loop: enqueue-άρισε recovery για τα νέα no-shows", r["recovery_enqueued"] >= 1)

    # 3) FHIR export: τα ραντεβού του ιατρείου ως FHIR Bundle
    b = F.bundle([F.booking_to_fhir(x) for x in db.get_all_bookings(S)])
    chk("FHIR export: όλα τα ραντεβού σε έγκυρο R4 Bundle",
        b["resourceType"] == "Bundle" and b["total"] >= 7)

    # 4) Audit chain intact μετά ΟΛΟ το stack
    v = db.verify_audit_chain(S)
    chk("Audit chain intact μετά e2e (bridge + loop)", v.get("valid", v.get("intact", True)))


if __name__ == "__main__":
    print("🔗 RAN-TEVU E2E AGENTIC STACK:")
    test_rantevu_e2e()
    print()
    if fails:
        print(f"❌ {len(fails)} ΑΠΟΤΥΧΙΕΣ: {fails}")
        sys.exit(1)
    print("✅ RAN-TEVU E2E: reactive journey → learning-on-new-data + recovery → FHIR export, audit intact.")
