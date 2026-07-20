"""
ΜΑΖΙΚΗ ΠΡΟΣΟΜΟΙΩΣΗ OMNIOS — χιλιάδες τυχαία επεισόδια λειτουργίας πάνω στον πραγματικό κώδικα.
==============================================================================================
Κάθε επεισόδιο = ένα ιατρείο με τυχαία ακολουθία: ασθενείς (με/χωρίς συναίνεση), ραντεβού,
completed/no-show/ακυρώσεις, waitlist, autopilot sweeps + ΑΥΤΟΝΟΜΗ εκτέλεση (policy on),
τιμολόγια/πληρωμές. Invariants που ΔΕΝ σπάνε ποτέ:
  I1 Καμία διπλοκράτηση ενεργών ραντεβού στο ίδιο slot
  I2 Completed → ΑΚΡΙΒΩΣ ένα auto-invoice ανά ραντεβού
  I3 Consent=False → ΚΑΜΙΑ πρόταση/ενέργεια επικοινωνίας για τον ασθενή
  I4 Approve idempotent — μία εκτέλεση ανά proposal, ακόμα και με διπλά κλικ
  I5 Audit hash-chain άθικτη μετά από χιλιάδες events
  I6 Metrics = ό,τι λένε τα πραγματικά δεδομένα (όχι εφευρέσεις)
Τρέξιμο: python core/test_mass_simulation.py [episodes]
"""
import random
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import datetime as dt

from audit import AUDIT
from clinic_store import ClinicStore
from clinic_autopilot import ClinicAutopilot
from autonomy_policy import AutonomyPolicy

EPISODES = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
TIMES = [f"{h:02d}:{m:02d}" for h in range(9, 20) for m in (0, 30)]

stats = {"ops": 0, "appts": 0, "conflicts": 0, "completed": 0, "noshow": 0, "cancel": 0,
         "backfill": 0, "sweeps": 0, "auto_exec": 0, "double_approve_blocked": 0}
violations = []


def D(rng, lo=-30, hi=14):
    return (dt.date.today() + dt.timedelta(days=rng.randint(lo, hi))).isoformat()


def episode(ep: int):
    rng = random.Random(5000 + ep)
    cs = ClinicStore(":memory:", tenant=f"sim-{ep}")
    cs.setup_clinic(name=f"Ιατρείο {ep}", default_value=rng.choice([40, 50, 60]))
    ap = ClinicAutopilot(cs)
    no_consent_ids = set()
    # ασθενείς: 20% ΧΩΡΙΣ συναίνεση
    pats = []
    for i in range(rng.randint(3, 8)):
        consent = rng.random() > 0.2
        p = cs.add_patient(f"Α{ep}-{i}", phone=f"69{80000000+i}", consent=consent)
        pats.append(p)
        if not consent:
            no_consent_ids.add(p["id"])
    # ραντεβού: τυχαία slots, παρελθόν+μέλλον
    appts = []
    for _ in range(rng.randint(6, 18)):
        stats["ops"] += 1
        p = rng.choice(pats)
        try:
            a = cs.add_appointment(rng.choice(TIMES), p["name"], date=D(rng),
                                   status="confirmed", patient_id=p["id"])
            appts.append(a)
            stats["appts"] += 1
        except ValueError:
            stats["conflicts"] += 1          # σωστή άρνηση διπλοκράτησης
    # transitions
    for a in list(appts):
        r = rng.random()
        stats["ops"] += 1
        if r < 0.35:
            cs.set_status(a["id"], "completed"); stats["completed"] += 1
        elif r < 0.5:
            cs.set_status(a["id"], "no_show"); stats["noshow"] += 1
        elif r < 0.6:
            if rng.random() < 0.5:
                cs.add_waitlist(rng.choice(pats)["name"])
            out = cs.cancel_appointment(a["id"])
            stats["cancel"] += 1
            if out.get("backfilled"):
                stats["backfill"] += 1
    # autopilot: policy ενεργή στα μισά επεισόδια → ΑΥΤΟΝΟΜΗ εκτέλεση
    pol = AutonomyPolicy(cs)
    if rng.random() < 0.5:
        rules = {k: {**v, "auto": True} for k, v in pol.get()["rules"].items()}
        pol.set(enabled=True, rules=rules)
    res = ap.run_autonomous()
    stats["sweeps"] += 1
    stats["auto_exec"] += res.get("executed_auto", 0) if isinstance(res.get("executed_auto"), int) \
        else len(res.get("executed_auto", []))
    # I4: διπλό approve σε τυχαίο pending
    pend = ap.pending()
    if pend:
        pid = pend[0]["id"]
        a1 = ap.approve(pid, approver="γιατρός")
        a2 = ap.approve(pid, approver="γιατρός")
        if a1["executed"] != a2["executed"]:
            violations.append(f"ep{ep} I4 διπλή εκτέλεση: {a1['executed']} != {a2['executed']}")
        else:
            stats["double_approve_blocked"] += 1
    # πληρωμές
    for inv in cs.list_invoices():
        if rng.random() < 0.4:
            stats["ops"] += 1
            cs.pay_invoice(inv["id"])

    # ─── INVARIANTS ───
    active = [x for x in cs.list_appointments() if x["status"] in ("booked", "confirmed", "arrived", "completed")]
    slots = [(x["date"], x["time"]) for x in active]
    if len(slots) != len(set(slots)):
        violations.append(f"ep{ep} I1 ΔΙΠΛΟΚΡΑΤΗΣΗ ενεργών slots")
    invs = cs.list_invoices()
    for a in cs.list_appointments():
        if a["status"] == "completed":
            n = sum(1 for i in invs if i.get("appointment_id") == a["id"])
            if n != 1:
                violations.append(f"ep{ep} I2 completed {a['id']} έχει {n} invoices (≠1)")
                break
    # I3: κανένα proposal για ασθενή χωρίς συναίνεση (πλην billing)
    for p in ap.all():
        if p.get("patient_id") in no_consent_ids and p["type"] != "billing" \
                and p["status"] in ("pending", "approved"):
            violations.append(f"ep{ep} I3 πρόταση επικοινωνίας ΧΩΡΙΣ συναίνεση: {p['type']}")
            break
    # I6: metrics συνεπή
    m = cs.metrics()
    real_today = len([a for a in cs.list_appointments()
                      if a["date"] == dt.date.today().isoformat() and a["status"] != "cancelled"])
    if m["appointments_today"] != real_today:
        violations.append(f"ep{ep} I6 metrics λένε {m['appointments_today']}, πραγματικά {real_today}")


def main():
    print(f"🤖 OMNIOS ΜΑΖΙΚΗ ΠΡΟΣΟΜΟΙΩΣΗ: {EPISODES} επεισόδια (τυχαία ιατρεία, autopilot on/off)…")
    for ep in range(EPISODES):
        try:
            episode(ep)
        except Exception as e:
            violations.append(f"ep{ep} CRASH: {type(e).__name__}: {e}")
        if ep % 200 == 0 and ep:
            print(f"  … επεισόδιο {ep}: {stats['ops']} πράξεις, violations: {len(violations)}")
    # I5: η ΚΟΙΝΗ audit chain άθικτη μετά από όλα
    intact = AUDIT.verify()
    print(f"\n{'═'*64}")
    print(f"ΠΡΑΞΕΙΣ: {stats['ops']} | ραντεβού {stats['appts']} (συγκρούσεις απορρίφθηκαν: {stats['conflicts']})"
          f" | completed {stats['completed']} | no-show {stats['noshow']} | ακυρώσεις {stats['cancel']}"
          f" → backfilled {stats['backfill']} | sweeps {stats['sweeps']} | αυτόνομες εκτελέσεις {stats['auto_exec']}"
          f" | διπλά-approve μπλοκαρισμένα {stats['double_approve_blocked']} | audit intact: {intact}")
    if not intact:
        violations.append("I5 audit chain ΣΠΑΣΜΕΝΗ")
    if violations:
        print(f"\n❌ {len(violations)} ΠΑΡΑΒΙΑΣΕΙΣ:")
        for v in violations[:12]:
            print("  •", v)
        sys.exit(1)
    print(f"✅ {EPISODES} ΕΠΕΙΣΟΔΙΑ ΧΩΡΙΣ ΚΑΜΙΑ ΠΑΡΑΒΙΑΣΗ — slots, τιμολόγια, συναίνεση, "
          f"idempotency, audit chain: όλα κράτησαν.")


if __name__ == "__main__":
    main()
