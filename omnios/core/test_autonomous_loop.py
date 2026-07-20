"""
test_autonomous_loop.py — ο αυτόνομος βρόχος ΜΑΘΑΙΝΕΙ πάνω σε νέα δεδομένα (5 layers).
Αποδεικνύει: το no-show estimate κινείται από τον prior προς τον ΑΛΗΘΙΝΟ ρυθμό καθώς φτάνουν outcomes·
το watermark μετράει κάθε outcome ΜΙΑ φορά (idempotent)· το μαθημένο state ΕΠΙΒΙΩΝΕΙ restart·
οι αποφάσεις προσαρμόζονται. Τρέχει: python core/test_autonomous_loop.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clinic_store import ClinicStore
from clinic_autopilot import ClinicAutopilot
from autonomy_policy import AutonomyPolicy
from autonomous_loop import AutonomousLoop
from datetime import datetime, timedelta

fails = []
def chk(label, ok):
    print(f"  {'✅' if ok else '❌ ΣΚΑΣΕ'} {label}")
    if not ok:
        fails.append(label)


def _past(days):
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def test_learns_toward_true_rate():
    """Αληθινός ρυθμός no-show ~60%. Ο learner ξεκινά στον prior 0.23 και ΚΙΝΕΙΤΑΙ προς τα πάνω."""
    cs = ClinicStore(":memory:", tenant="loop-1")
    cs.setup_clinic(name="Ι", specialty="Φ", default_value=40.0)
    p = cs.add_patient("Α", phone="6970000001", consent=True)
    loop = AutonomousLoop(cs)
    start = loop.learner.no_show_prob()
    chk("Ξεκινά στον prior (~0.23)", abs(start - 0.23) < 0.02)

    # Τροφοδότησε 30 ολοκληρωμένα outcomes με 60% no-show, σε παρτίδες (νέα δεδομένα ανά κύκλο)
    import random
    rng = random.Random(42)
    day = 1
    for batch in range(3):
        for _ in range(10):
            a = cs.add_appointment(f"{9 + (day % 8)}:00", "Α", "Συνεδρία", _past(day), 40.0, "confirmed", p["id"])
            cs.set_status(a["id"], "no_show" if rng.random() < 0.6 else "completed")
            day += 1
        loop.cycle()

    learned = loop.learner.no_show_prob()
    chk(f"Έμαθε προς τον αληθινό ~0.60 (learned={learned:.2f}, από 0.23)", learned > start + 0.15)
    chk("Παρατήρησε και τα 30 outcomes", loop.learner.n == 30)


def test_watermark_idempotent():
    cs = ClinicStore(":memory:", tenant="loop-2")
    cs.setup_clinic(name="Ι", specialty="Φ", default_value=40.0)
    p = cs.add_patient("Β", phone="6970000002", consent=True)
    a = cs.add_appointment("10:00", "Β", "Συνεδρία", _past(2), 40.0, "confirmed", p["id"])
    cs.set_status(a["id"], "no_show")
    loop = AutonomousLoop(cs)
    r1 = loop.cycle()
    r2 = loop.cycle()   # κανένα νέο outcome
    chk("1ος κύκλος: 1 νέο outcome· 2ος κύκλος: 0 (watermark)", r1["observed_new"] == 1 and r2["observed_new"] == 0)
    chk("Το rate δεν αλλάζει χωρίς νέα δεδομένα", r1["learned_no_show_after"] == r2["learned_no_show_after"])


def test_state_survives_restart():
    cs = ClinicStore(":memory:", tenant="loop-3")
    cs.setup_clinic(name="Ι", specialty="Φ", default_value=40.0)
    p = cs.add_patient("Γ", phone="6970000003", consent=True)
    for k in range(5):
        a = cs.add_appointment(f"{9 + k}:00", "Γ", "Συνεδρία", _past(k + 1), 40.0, "confirmed", p["id"])
        cs.set_status(a["id"], "no_show")
    AutonomousLoop(cs).cycle()
    # «restart»: νέο AutonomousLoop στο ΙΔΙΟ store → φορτώνει το μαθημένο state
    loop2 = AutonomousLoop(cs)
    chk("Το μαθημένο state επιβίωσε (n=5 μετά restart)", loop2.learner.n == 5)
    r = loop2.cycle()
    chk("Δεν ξανα-μαθαίνει τα ίδια (watermark persisted)", r["observed_new"] == 0)


def test_autonomous_execution_within_loop():
    cs = ClinicStore(":memory:", tenant="loop-4")
    cs.setup_clinic(name="Ι", specialty="Φ", default_value=40.0)
    p = cs.add_patient("Δ", phone="6970000004", consent=True)
    cs.add_appointment("10:00", "Δ", "Συνεδρία", _past(1), 40.0, "no_show", p["id"])   # → recovery
    ap = ClinicAutopilot(cs)
    AutonomyPolicy(cs).set(enabled=True, rules={"no_show_recovery": {"auto": True, "max_eur": 200}}, set_by="dr")
    loop = AutonomousLoop(cs, autopilot=ap)
    r = loop.cycle()
    chk("L4: ο βρόχος εκτέλεσε αυτόνομα την εξουσιοδοτημένη δουλειά (dispatched≥1)", r["dispatched"] >= 1)
    chk("L1/L2: έμαθε από το no_show (n≥1, rate ανέβηκε)",
        r["total_observations"] >= 1 and r["learned_no_show_after"] >= r["learned_no_show_before"])


if __name__ == "__main__":
    print("🔄 AUTONOMOUS LEARNING LOOP — synthesize · learn · execute σε layers:")
    test_learns_toward_true_rate()
    test_watermark_idempotent()
    test_state_survives_restart()
    test_autonomous_execution_within_loop()
    print()
    if fails:
        print(f"❌ {len(fails)} ΑΠΟΤΥΧΙΕΣ: {fails}")
        sys.exit(1)
    print("✅ AUTONOMOUS LOOP: μαθαίνει προς τον αληθινό ρυθμό, idempotent watermark, επιβιώνει restart,")
    print("   εκτελεί αυτόνομα εντός policy — αυτο-βελτιούμενο σύστημα σε νέα δεδομένα.")
