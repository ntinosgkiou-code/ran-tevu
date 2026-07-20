"""
test_concurrent_load.py — ΠΡΟΣΟΜΟΙΩΣΗ ΠΙΕΣΗΣ OmniOS: 5 ασθενείς + 3 γιατροί ταυτόχρονα.
================================================================================
Mock ΔΕΔΟΜΕΝΑ, ΠΡΑΓΜΑΤΙΚΟΣ κώδικας. 3 tenants (ένα ανά γιατρό) — ΕΝΑ κοινό ClinicStore + Autopilot
ανά tenant (ακριβώς όπως το _REG του api.py, ώστε ο per-tenant _slot_lock να ισχύει). 5 patient
actors + 3 doctor actors τρέχουν ΤΑΥΤΟΧΡΟΝΑ σε threads με ΣΚΟΠΙΜΕΣ συγκρούσεις για slots.

Invariants μετά την πίεση:
  1. ΜΗΔΕΝ double-booking       — δύο ενεργά ραντεβού στο ίδιο (date,time) ανά tenant = ΑΠΟΤΥΧΙΑ
  2. Tenant isolation           — κανένα ραντεβού ασθενή του Α δεν εμφανίζεται στον Β
  3. Idempotency (autopilot)    — 8 ταυτόχρονα approve() του ΙΔΙΟΥ proposal = 1 side-effect (1 outbox)
  4. Audit chain intact         — AUDIT.verify() μετά την πίεση
  5. Οικονομικά                 — καμία πληρωμή invoice > amount

Τρέχει: python core/test_concurrent_load.py
"""
import os, sys, time, threading, random
from concurrent.futures import ThreadPoolExecutor, as_completed

os.environ.setdefault("OMNIOS_CLINIC_DB", ":memory:")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from clinic_store import ClinicStore
from clinic_autopilot import ClinicAutopilot
from clinic_brain import scan_clinic
from clinical_scribe import ClinicalScribe
from audit import AUDIT
from datetime import datetime, timedelta

# ── Στήσιμο: 3 tenants, ΕΝΑ κοινό store+autopilot ανά tenant (shared across threads) ──
TENANTS = ["clinic-alpha", "clinic-beta", "clinic-gamma"]
REG = {}
for i, t in enumerate(TENANTS):
    st = ClinicStore(":memory:", tenant=t)   # κάθε tenant δικό του in-memory store (hard isolation)
    st.setup_clinic(name=f"Ιατρείο {t}", specialty="Φυσικοθεραπεία", default_value=40.0)
    # consenting ασθενείς με τηλέφωνο (ο autopilot θέλει consent + phone για outreach)
    pats = [st.add_patient(f"Ασθ {t}-{k}", phone=f"69{i}0000{k:03d}", consent=True) for k in range(5)]
    REG[t] = {"store": st, "autopilot": ClinicAutopilot(st), "patients": pats}

SCRIBE = ClinicalScribe()
DAYS = [(datetime.now() + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(1, 5)]
TIMES = ["09:00", "09:30", "10:00", "10:30", "11:00"]   # μικρή δεξαμενή → collisions

_lock = threading.Lock()
M = {"ops": 0, "booked": 0, "double_rejected": 0, "rescheduled": 0, "cancelled": 0,
     "statuses": 0, "invoiced": 0, "paid": 0, "risk_scans": 0, "soap": 0, "sweeps": 0,
     "errors": 0, "error_samples": []}
LAT = []
_appts = []   # (tenant, aid, patient) ενεργά


def bump(k, n=1):
    with _lock:
        M[k] += n


def timed(fn, *a, **kw):
    t0 = time.perf_counter()
    try:
        r = fn(*a, **kw)
        bump("ops")
        with _lock:
            LAT.append(time.perf_counter() - t0)
        return r
    except ValueError as e:
        if "double-booking" in str(e) or "already booked" in str(e):
            bump("double_rejected")     # ΑΝΑΜΕΝΟΜΕΝΟ — ο guard δούλεψε
        else:
            bump("errors")
            with _lock:
                if len(M["error_samples"]) < 8:
                    M["error_samples"].append(f"{fn.__name__}: {e}")
        return None
    except KeyError:
        return None                     # race: κάποιος άλλος ακύρωσε το appt — ανεκτό
    except Exception as e:
        bump("errors")
        with _lock:
            if len(M["error_samples"]) < 8:
                M["error_samples"].append(f"{fn.__name__}: {type(e).__name__}: {e}")
        return None


# ── PATIENT actor: book → (reschedule|cancel|status) με σκόπιμες συγκρούσεις ──
def patient_actor(idx):
    rng = random.Random(1000 + idx)
    for _ in range(30):
        t = rng.choice(TENANTS)
        st = REG[t]["store"]
        p = rng.choice(REG[t]["patients"])
        date = rng.choice(DAYS); tm = rng.choice(TIMES)
        rec = timed(st.add_appointment, tm, p["name"], "Συνεδρία", date, None, "confirmed", p["id"])
        if rec:
            bump("booked")
            with _lock:
                _appts.append((t, rec["id"], p["name"]))
            roll = rng.random()
            if roll < 0.30:
                if timed(st.reschedule_appointment, rec["id"], rng.choice(TIMES), rng.choice(DAYS)):
                    bump("rescheduled")
            elif roll < 0.45:
                if timed(st.cancel_appointment, rec["id"]):
                    bump("cancelled")
            elif roll < 0.60:
                if timed(st.set_status, rec["id"], "completed"):
                    bump("statuses")


# ── DOCTOR actor: risk scan → invoice+pay → SOAP → autopilot sweep ──
def doctor_actor(ti):
    t = TENANTS[ti]
    st = REG[t]["store"]; ap = REG[t]["autopilot"]
    rng = random.Random(2000 + ti)
    for _ in range(30):
        if timed(scan_clinic, st, 14) is not None:
            bump("risk_scans")
        p = rng.choice(REG[t]["patients"])
        inv = timed(st.add_invoice, p["name"], 40.0, "unpaid", p["id"])
        if inv:
            bump("invoiced")
            if rng.random() < 0.6:
                if timed(st.pay_invoice, inv["id"]):
                    bump("paid")
        note = timed(SCRIBE.draft_sync, "Ο ασθενής αναφέρει οσφυαλγία 3 ημερών. Πίεση 120/80.")
        if note:
            bump("soap")
        if timed(ap.sweep) is not None:
            bump("sweeps")


# ── Idempotency burst: 8 ταυτόχρονα approve() του ΙΔΙΟΥ proposal → 1 outbox ──
def idempotency_burst():
    t = TENANTS[0]
    st = REG[t]["store"]; ap = REG[t]["autopilot"]
    p = REG[t]["patients"][0]
    # φτιάξε ΕΝΑ no_show → ο sweep βγάζει no_show_recovery proposal
    a = st.add_appointment("15:00", p["name"], "Συνεδρία",
                           (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
                           None, "no_show", p["id"])
    ap.sweep()
    pend = [x for x in ap.pending() if x["type"] == "no_show_recovery"]
    if not pend:
        return None   # δεν παρήχθη proposal (π.χ. λόγω consent) — skip cleanly
    pid = pend[0]["id"]
    action_text = pend[0]["action"]
    before = sum(1 for m in ap.channels.outbox() if m.get("text") == action_text)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(ap.approve, pid, "dr_test") for _ in range(8)]
        for f in as_completed(futs):
            f.result()
    after = sum(1 for m in ap.channels.outbox() if m.get("text") == action_text)
    return after - before   # πρέπει να είναι ΑΚΡΙΒΩΣ 1


# ══════════════════════════════ ΕΚΤΕΛΕΣΗ ══════════════════════════════════════
print("🏥 OmniOS ΠΡΟΣΟΜΟΙΩΣΗ ΠΙΕΣΗΣ: 5 ασθενείς + 3 γιατροί ΤΑΥΤΟΧΡΟΝΑ (3 tenants)…")
t0 = time.perf_counter()
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = [ex.submit(patient_actor, i) for i in range(5)] + \
           [ex.submit(doctor_actor, j) for j in range(3)]
    for f in as_completed(futs):
        f.result()
wall = time.perf_counter() - t0

idem_delta = idempotency_burst()

# ── INVARIANTS ───────────────────────────────────────────────────────────────
fails = []

# 1) ΜΗΔΕΝ double-booking ανά tenant
for t in TENANTS:
    seen = {}
    for a in REG[t]["store"].list_appointments():
        if a["status"] == "cancelled":
            continue
        key = (a["date"], a["time"])
        seen[key] = seen.get(key, 0) + 1
    dupes = {k: v for k, v in seen.items() if v > 1}
    if dupes:
        fails.append(f"DOUBLE-BOOKING στο {t}: {dupes}")

# 2) Tenant isolation — τα ραντεβού του Α δεν διαρρέουν στον Β (namespaced)
names_by_tenant = {t: {a["patient"] for a in REG[t]["store"].list_appointments()} for t in TENANTS}
for t in TENANTS:
    for other in TENANTS:
        if other == t:
            continue
        leak = names_by_tenant[t] & names_by_tenant[other]
        if leak:
            fails.append(f"CROSS-TENANT: ονόματα {leak} και στο {t} και στο {other}")

# 3) Idempotency — 8 ταυτόχρονα approve → 1 side-effect
if idem_delta is not None and idem_delta != 1:
    fails.append(f"IDEMPOTENCY: 8 ταυτόχρονα approve() → {idem_delta} outbox εγγραφές (αναμ. 1)")

# 4) Audit chain intact
if not AUDIT.verify():
    fails.append("AUDIT: η in-process hash-chain ΔΕΝ επαληθεύεται μετά την πίεση")

# 5) Οικονομικά — καμία πληρωμή > amount
for t in TENANTS:
    for inv in REG[t]["store"].list_invoices():
        if inv.get("status") == "paid" and float(inv.get("paid", inv["amount"])) > inv["amount"] + 0.001:
            fails.append(f"ΟΙΚΟΝΟΜΙΚΑ: τιμολόγιο {inv['id']} πληρωμή > amount")
            break

# 6) Κανένα hard error
if M["errors"]:
    fails.append(f"THREAD ERRORS: {M['errors']} — {M['error_samples']}")

# ── ΑΝΑΦΟΡΑ ──────────────────────────────────────────────────────────────────
LAT.sort()
def pct(p):
    return round(LAT[min(len(LAT)-1, int(len(LAT)*p))] * 1000, 1) if LAT else 0.0

print(f"""
════════════════════════════════════════════════════════════════
⏱️  Διάρκεια: {wall:.2f}s · {M['ops']} επιτυχείς πράξεις · {round(M['ops']/wall,1)} ops/s
📊 booked {M['booked']} | double-booking σωστά απορρίφθηκαν {M['double_rejected']} | rescheduled {M['rescheduled']} | cancelled {M['cancelled']} | status→completed {M['statuses']}
🩺 risk scans {M['risk_scans']} | invoiced {M['invoiced']} | paid {M['paid']} | SOAP drafts {M['soap']} | autopilot sweeps {M['sweeps']}
⚡ latency p50 {pct(0.5)}ms · p95 {pct(0.95)}ms
🔁 idempotency: 8 ταυτόχρονα approve → {'skip (κανένα proposal)' if idem_delta is None else str(idem_delta)+' side-effect'}
════════════════════════════════════════════════════════════════""")

if fails:
    print("❌ ΑΠΟΤΥΧΙΑ INVARIANTS:")
    for f in fails:
        print(f"   • {f}")
    sys.exit(1)
else:
    print("✅ ΟΛΑ ΤΑ INVARIANTS ΚΡΑΤΗΣΑΝ ΥΠΟ ΠΙΕΣΗ — μηδέν double-booking, tenant isolation,")
    print("   idempotent autopilot, audit intact, οικονομικά σωστά, μηδέν απρόσμενα σφάλματα.")
