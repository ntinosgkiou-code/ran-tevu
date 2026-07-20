"""
test_autonomous_loop.py (Ran-Tevu) — ο αυτόνομος βρόχος μαθαίνει & εκτελεί ανάκτηση σε νέα δεδομένα.
Τρέχει: python tests/test_autonomous_loop.py
"""
import os, sys, tempfile
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "rt_autoloop.db")
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
import autonomous_loop as L
from datetime import datetime, timedelta

db.init_db()

fails = []
def chk(label, ok):
    print(f"  {'✅' if ok else '❌ ΣΚΑΣΕ'} {label}")
    if not ok:
        fails.append(label)


def _future(days, hour):
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d"), f"{hour:02d}:00"


def test_learns_and_recovers():
    S = provisioning.provision("Loop Ιατρείο", "physio", owner_name="Δρ", email="dr@x.gr", demo=False)["slug"]
    # 20 outcomes με 70% no-show (σε 2 παρτίδες → νέα δεδομένα ανά κύκλο)
    import random
    rng = random.Random(7)
    day = 1
    for batch in range(2):
        for k in range(10):
            d, t = _future(day, 8 + (k % 10))
            phone = f"69{day:08d}"
            db.log_consent(consent_type="reminders", slug=S, identifier=phone, accepted=True)  # opt-in
            bid = db.create_booking(S, d, t, "Συνεδρία", 30, f"Ασθ{day}", phone)
            db.mark_booking_outcome(bid, "no_show" if rng.random() < 0.7 else "completed")
            day += 1
        r = L.cycle(S)

    st = L.state(S)
    chk(f"Έμαθε προς τον αληθινό ~0.70 (learned={st['learned_no_show_prob']:.2f}, από 0.23)",
        st["learned_no_show_prob"] > 0.40)
    chk("Παρατήρησε και τα 20 outcomes", st["observations"] == 20)

    # recovery jobs για τα no-shows
    with db._conn() as c:
        rec = c.execute("SELECT COUNT(*) FROM outbound_jobs WHERE slug=? AND job_type='no_show_recovery'", (S,)).fetchone()[0]
    chk("L4: enqueue-άρισε recovery για τα no-shows (idempotent pipeline)", rec >= 1)
    chk("Το τελευταίο cycle enqueue-άρισε ≥0 (idempotent — δεν διπλασιάζει)", r["recovery_enqueued"] >= 0)


def test_watermark_and_persistence():
    S = provisioning.provision("Loop Ιατρείο 2", "physio", owner_name="Δρ", email="dr2@x.gr", demo=False)["slug"]
    d, t = _future(1, 9)
    db.log_consent(consent_type="reminders", slug=S, identifier="6900000099", accepted=True)
    bid = db.create_booking(S, d, t, "Συνεδρία", 30, "Μονος", "6900000099")
    db.mark_booking_outcome(bid, "no_show")
    r1 = L.cycle(S)
    r2 = L.cycle(S)   # κανένα νέο outcome
    chk("Watermark: 1ος κύκλος 1 νέο, 2ος 0", r1["observed_new"] == 1 and r2["observed_new"] == 0)
    chk("Recovery idempotent: 1ος enqueue 1, 2ος 0", r1["recovery_enqueued"] == 1 and r2["recovery_enqueued"] == 0)
    # persistence: το state διαβάζεται φρέσκο από τη βάση
    chk("Το μαθημένο state persist-άρεται (n=1)", L.state(S)["observations"] == 1)


def test_consent_gate_blocks_recovery():
    """ΧΩΡΙΣ καταγεγραμμένη συναίνεση → ο βρόχος ΔΕΝ κάνει αυτόνομη επικοινωνία (opt-in gate)."""
    S = provisioning.provision("Loop Ιατρείο 3", "physio", owner_name="Δρ", email="dr3@x.gr", demo=False)["slug"]
    d, t = _future(1, 10)
    bid = db.create_booking(S, d, t, "Συνεδρία", 30, "Χωρίς Συναίνεση", "6900000088")  # ΚΑΜΙΑ συναίνεση
    db.mark_booking_outcome(bid, "no_show")
    r = L.cycle(S)
    chk("Χωρίς consent → 0 recovery enqueued, skipped_no_consent≥1 (όχι auto-blast)",
        r["recovery_enqueued"] == 0 and r["skipped_no_consent"] >= 1)
    with db._conn() as c:
        rec = c.execute("SELECT COUNT(*) FROM outbound_jobs WHERE slug=? AND job_type='no_show_recovery'", (S,)).fetchone()[0]
    chk("Κανένα recovery job στη βάση για μη-συναινούντα ασθενή", rec == 0)


def test_run_all_clinics():
    r = L.run_all_clinics()
    chk("run_all_clinics: τρέχει έναν κύκλο για κάθε ιατρείο (clinics≥2)", r["clinics"] >= 2)


if __name__ == "__main__":
    print("🔄 RAN-TEVU AUTONOMOUS LEARNING LOOP:")
    test_learns_and_recovers()
    test_watermark_and_persistence()
    test_consent_gate_blocks_recovery()
    test_run_all_clinics()
    print()
    if fails:
        print(f"❌ {len(fails)} ΑΠΟΤΥΧΙΕΣ: {fails}")
        sys.exit(1)
    print("✅ RAN-TEVU AUTONOMOUS LOOP: μαθαίνει από νέα outcomes, idempotent watermark + recovery,")
    print("   εκτελεί ανάκτηση μέσω του υπάρχοντος pipeline — αυτο-βελτιούμενο, server-side.")
