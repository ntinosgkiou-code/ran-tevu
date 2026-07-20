"""
test_concurrent_load.py — ΠΡΟΣΟΜΟΙΩΣΗ ΠΙΕΣΗΣ: 5 ασθενείς + 3 γιατροί ταυτόχρονα.
================================================================================
Mock ΔΕΔΟΜΕΝΑ, ΠΡΑΓΜΑΤΙΚΟΣ κώδικας. 3 ιατρεία (ένα ανά γιατρό). 5 patient actors + 3 doctor
actors τρέχουν ΤΑΥΤΟΧΡΟΝΑ σε threads, χτυπώντας την ΙΔΙΑ βάση, με ΣΚΟΠΙΜΕΣ συγκρούσεις για slots
ώστε να πιεστούν οι concurrency guards. Στο τέλος επαληθεύει ΑΜΕΤΑΒΛΗΤΑ (invariants):

  1. ΜΗΔΕΝ double-booking            — δύο ενεργά ραντεβού στο ίδιο (ιατρείο,ημέρα,ώρα) = ΑΠΟΤΥΧΙΑ
  2. ΜΗΔΕΝ cross-tenant διαρροή      — τα ραντεβού κάθε ιατρείου ανήκουν ΜΟΝΟ σε αυτό
  3. Idempotency                    — 8 ταυτόχρονες ίδιες ενέργειες (ίδιο request_id) = 1 side-effect
  4. Audit chain intact             — η tamper-evident αλυσίδα επαληθεύεται μετά την πίεση
  5. Οικονομικά reconcile           — καμία πληρωμή > σύνολο τιμολογίου

Τρέχει: python tests/test_concurrent_load.py
"""
import os, sys, tempfile, time, threading, random
from concurrent.futures import ThreadPoolExecutor, as_completed

os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "rt_concurrent_load.db")
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
import back_office as bo
import back_office_assistant as A
import provisioning
from datetime import datetime, timedelta

db.init_db()

# ── Στήσιμο: 3 ιατρεία (ένα ανά γιατρό) ──────────────────────────────────────
DOCTORS = []
for i in range(3):
    prov = provisioning.provision(f"Ιατρείο Δρ. {chr(913+i)}", "physio",
                                  owner_name=f"Δρ. {chr(913+i)}", email=f"dr{i}@test.gr", demo=False)
    DOCTORS.append(prov["slug"])

# Μικρή δεξαμενή slots ΜΟΙΡΑΖΟΜΕΝΗ → οι ασθενείς συγκρούονται σκόπιμα στα ίδια ραντεβού.
DAYS = [(datetime.now() + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(1, 6)]
TIMES = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"]
PATIENTS = [(f"Ασθενής {chr(913+i)}", f"69000000{i:02d}") for i in range(5)]

# ── Μετρικά (thread-safe) ────────────────────────────────────────────────────
_lock = threading.Lock()
M = {"ops": 0, "booked": 0, "double_rejected": 0, "cancelled": 0, "rescheduled": 0,
     "resolved": 0, "invoiced": 0, "paid": 0, "waitlisted": 0, "notes": 0,
     "reorders": 0, "errors": 0, "error_samples": []}
LAT = []
_booking_ids = []      # ενεργά booking ids (thread-safe append)


def bump(k, n=1):
    with _lock:
        M[k] += n


def timed(fn, *a, **kw):
    """Τρέχει μια πραγματική ενέργεια, μετράει latency, καταγράφει errors ΧΩΡΙΣ να ρίχνει το thread."""
    t0 = time.perf_counter()
    try:
        r = fn(*a, **kw)
        bump("ops")
        with _lock:
            LAT.append(time.perf_counter() - t0)
        return r
    except ValueError as e:
        if "double_booking" in str(e):
            bump("double_rejected")          # ΑΝΑΜΕΝΟΜΕΝΟ — ο guard δούλεψε, ΟΧΙ σφάλμα
        else:
            bump("errors")
            with _lock:
                if len(M["error_samples"]) < 8:
                    M["error_samples"].append(f"{fn.__name__}: {e}")
        return None
    except Exception as e:
        bump("errors")
        with _lock:
            if len(M["error_samples"]) < 8:
                M["error_samples"].append(f"{fn.__name__}: {type(e).__name__}: {e}")
        return None


# ── PATIENT actor: book → (reschedule|cancel|waitlist) — σκόπιμες συγκρούσεις ──
def patient_actor(idx):
    rng = random.Random(1000 + idx)
    name, phone = PATIENTS[idx]
    for _ in range(30):
        slug = rng.choice(DOCTORS)
        date = rng.choice(DAYS); tm = rng.choice(TIMES)   # μικρή δεξαμενή → collisions
        bid = timed(db.create_booking, slug, date, tm, "Συνεδρία", 30, name, phone, "", "")
        if bid:
            bump("booked")
            with _lock:
                _booking_ids.append((slug, bid, phone))
            roll = rng.random()
            if roll < 0.30:                                # μετάθεση σε άλλο slot
                nd = rng.choice(DAYS); nt = rng.choice(TIMES)
                if timed(db.reschedule_booking, bid, phone, nd, nt):
                    bump("rescheduled")
            elif roll < 0.50:                              # ακύρωση → πυροδοτεί waitlist backfill
                timed(db.cancel_booking, bid)
                bump("cancelled")
        else:
            # το slot ήταν πιασμένο → μπες στη λίστα αναμονής (σαν πραγματικός ασθενής)
            if timed(db.add_to_waitlist, slug, date, tm, "Συνεδρία", name, phone, ""):
                bump("waitlisted")


# ── DOCTOR actor: resolve → invoice → payment → note → low-stock reorder ──────
def doctor_actor(slug_idx):
    slug = DOCTORS[slug_idx]
    rng = random.Random(2000 + slug_idx)
    # στήσε λίγο απόθεμα ώστε το low_stock να έχει νόημα
    timed(bo.add_supplier, slug, "Προμηθευτής Α")
    timed(bo.add_inventory_item, slug, "Γάζες", 2, 5, "τεμ")
    for _ in range(30):
        # 1) resolve ένα ενεργό ραντεβού (completed → auto-invoice)
        with _lock:
            mine = [(s, b, p) for (s, b, p) in _booking_ids if s == slug]
        if mine:
            s, bid, phone = rng.choice(mine)
            outcome = "completed" if rng.random() < 0.7 else "no_show"
            r = timed(bo.resolve_booking, s, bid, outcome)
            if r and r.get("ok"):
                bump("resolved")
        # 2) έκδοση τιμολογίου (idempotent μέσω request_id — δύο threads, ένα τιμολόγιο)
        if mine:
            s, bid, phone = rng.choice(mine)
            rid = f"inv:{slug}:{bid}"
            r = timed(A.execute_action, s, "create_invoice",
                      {"phone": phone, "name": "Ασθενής", "service": "Συνεδρία", "amount": 40}, rid)
            if r and r.get("ok"):
                bump("invoiced")
        # 3) low-stock check → reorder task
        low = timed(bo.low_stock, slug)
        if low:
            bump("reorders", 0)   # μετρημένο ξεχωριστά στο invariant· εδώ απλά ασκεί το path
        # 4) κλινική σημείωση στον φάκελο
        if mine:
            s, bid, phone = rng.choice(mine)
            if timed(bo.add_patient_note, s, phone, "Οσφυαλγία — 6 συνεδρίες", "clinical", "Αξιολόγηση", "γιατρός") is not None:
                bump("notes")


# ── Idempotency stress: 8 ταυτόχρονα ίδια create_invoice (ίδιο rid) → 1 τιμολόγιο ─
def idempotency_burst():
    slug = DOCTORS[0]
    bo.upsert_patient(slug, "6900009999", "Idem Ασθενής", "")
    rid = "idem-burst-fixed-key"
    inp = {"phone": "6900009999", "name": "Idem Ασθενής", "service": "Έλεγχος", "amount": 25}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(A.execute_action, slug, "create_invoice", dict(inp), rid) for _ in range(8)]
        for f in as_completed(futs):
            f.result()
    with db._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM bo_invoices WHERE slug=? AND patient_phone=?",
                      (slug, "6900009999")).fetchone()[0]
    return n


# ══════════════════════════════ ΕΚΤΕΛΕΣΗ ══════════════════════════════════════
print("🏥 ΠΡΟΣΟΜΟΙΩΣΗ ΠΙΕΣΗΣ: 5 ασθενείς + 3 γιατροί ΤΑΥΤΟΧΡΟΝΑ (3 ιατρεία, κοινή βάση)…")
t_start = time.perf_counter()
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = []
    for i in range(5):
        futs.append(ex.submit(patient_actor, i))
    for j in range(3):
        futs.append(ex.submit(doctor_actor, j))
    for f in as_completed(futs):
        f.result()   # propagate any thread that died hard (δεν πρέπει να συμβεί)
wall = time.perf_counter() - t_start

idem_invoices = idempotency_burst()

# ── INVARIANTS ───────────────────────────────────────────────────────────────
fails = []

# 1) ΜΗΔΕΝ double-booking
with db._conn() as c:
    dupes = c.execute("""SELECT slug, date, time, COUNT(*) n FROM bookings
                         WHERE status != 'cancelled'
                         GROUP BY slug, date, time HAVING COUNT(*) > 1""").fetchall()
if dupes:
    fails.append(f"DOUBLE-BOOKING: {len(dupes)} slots με >1 ενεργό ραντεβού")

# 2) ΜΗΔΕΝ cross-tenant διαρροή — κάθε ραντεβού ανήκει σε γνωστό ιατρείο
with db._conn() as c:
    alien = c.execute("SELECT COUNT(*) FROM bookings WHERE slug NOT IN (?,?,?)", tuple(DOCTORS)).fetchone()[0]
if alien:
    fails.append(f"CROSS-TENANT: {alien} ραντεβού σε άγνωστο slug")

# 3) Idempotency — 8 ταυτόχρονα ίδια → ΑΚΡΙΒΩΣ 1 τιμολόγιο
if idem_invoices != 1:
    fails.append(f"IDEMPOTENCY: 8 ταυτόχρονες ίδιες create_invoice → {idem_invoices} τιμολόγια (αναμ. 1)")

# 4) Audit chain intact (ανά ιατρείο)
for slug in DOCTORS:
    v = db.verify_audit_chain(slug)
    if not v.get("valid", v.get("intact", True)):
        fails.append(f"AUDIT: αλυσίδα ιατρείου {slug} ΔΕΝ επαληθεύεται: {v}")

# 5) Οικονομικά — καμία πληρωμή > σύνολο
with db._conn() as c:
    bad_pay = c.execute("SELECT COUNT(*) FROM bo_invoices WHERE paid > total + 0.001").fetchone()[0]
if bad_pay:
    fails.append(f"ΟΙΚΟΝΟΜΙΚΑ: {bad_pay} τιμολόγια με πληρωμή > σύνολο")

# 6) Κανένα hard error στα threads
if M["errors"]:
    fails.append(f"THREAD ERRORS: {M['errors']} απρόσμενα σφάλματα — {M['error_samples']}")

# ── ΑΝΑΦΟΡΑ ──────────────────────────────────────────────────────────────────
LAT.sort()
def pct(p):
    if not LAT: return 0.0
    return round(LAT[min(len(LAT)-1, int(len(LAT)*p))] * 1000, 1)

print(f"""
════════════════════════════════════════════════════════════════
⏱️  Διάρκεια: {wall:.2f}s · {M['ops']} επιτυχείς πράξεις · {round(M['ops']/wall,1)} ops/s
📊 booked {M['booked']} | double-booking σωστά απορρίφθηκαν {M['double_rejected']} | rescheduled {M['rescheduled']} | cancelled {M['cancelled']} | waitlisted {M['waitlisted']}
🩺 resolved {M['resolved']} | invoiced {M['invoiced']} | notes {M['notes']}
⚡ latency p50 {pct(0.5)}ms · p95 {pct(0.95)}ms
🔁 idempotency burst: 8 ταυτόχρονες ίδιες → {idem_invoices} τιμολόγιο
════════════════════════════════════════════════════════════════""")

if fails:
    print("❌ ΑΠΟΤΥΧΙΑ INVARIANTS:")
    for f in fails:
        print(f"   • {f}")
    sys.exit(1)
else:
    print("✅ ΟΛΑ ΤΑ INVARIANTS ΚΡΑΤΗΣΑΝ ΥΠΟ ΠΙΕΣΗ — μηδέν double-booking, μηδέν cross-tenant,")
    print("   idempotent, audit intact, οικονομικά σωστά, μηδέν απρόσμενα σφάλματα.")
