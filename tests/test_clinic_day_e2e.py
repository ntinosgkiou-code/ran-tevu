"""ΤΕΣΤ ΑΛΗΘΙΝΗΣ ΜΕΡΑΣ ΙΑΤΡΕΙΟΥ — end-to-end, ΠΡΑΓΜΑΤΙΚΟΣ κώδικας, ΠΡΑΓΜΑΤΙΚΗ βάση.
Αν αυτό περάσει, ένας γιατρός μπορεί να περάσει μια μέρα ΧΩΡΙΣ να εκτεθεί. Αν σκάσει, δείχνει ΠΟΥ.
Τρέχει: python tests/test_clinic_day_e2e.py
"""
import os, sys, tempfile
from datetime import datetime, timedelta

os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "rt_clinicday.db")
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
import provisioning
from sms_service import sms_reminder

TOM = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
fails = []
def step(n, ok, proof):
    print(f"  {'✅' if ok else '❌ ΣΚΑΣΕ'} {n}\n       → {proof}")
    if not ok:
        fails.append(n)

db.init_db()
# ΣΚΟΠΙΜΑ ΔΕΝ καλώ bo.init_back_office() εδώ — αποδεικνύω ότι το provision() στήνεται ΜΟΝΟ του
# (όπως όταν το σκανδαλίζει το Stripe webhook σε φρέσκο process που δεν πρόλαβε το startup init).

# 1) Ο γιατρός στήνεται ΜΟΝΟΣ (self-serve provision, ΚΑΘΑΡΟ — 0 fake data)
prov = provisioning.provision("Φυσικοθεραπευτήριο Δοκιμή", "φυσικοθεραπεία",
                              owner_name="Δρ. Παπάς", email="dr@test.gr", demo=False)
S = prov["slug"]
biz = db.get_business(S)
step("1. Γιατρός στήνεται μόνος (self-serve)",
     bool(biz) and bool(prov.get("links")),
     f"ιατρείο '{biz['name']}' live, slug={S}, links={len(prov.get('links',{}))} · 0 fake ασθενείς")

# 2) Ασθενής Α κλείνει ραντεβού αύριο 10:00
a = db.create_booking(S, TOM, "10:00", "Συνεδρία", 30, "Ασθενής Α", "6970000001")
today_appts = db.get_bookings_for_date(S, TOM)
step("2. Ασθενής κλείνει ραντεβού",
     bool(a) and any(b.get("time") == "10:00" for b in today_appts),
     f"booking #{a} στις 10:00 · το πρόγραμμα δείχνει {len(today_appts)} ραντεβού")

# 3) Η 10:00 είναι πιασμένη → Ασθενής Β μπαίνει στη λίστα αναμονής
wid = db.add_to_waitlist(S, TOM, "10:00", "Συνεδρία", "Ασθενής Β", "6970000002", "b@test.gr")
step("3. 2ος ασθενής μπαίνει σε waitlist", bool(wid), f"waitlist #{wid} για την 10:00")

# 4) Reminder path τρέχει χωρίς crash (SMS graceful χωρίς Twilio number)
try:
    pending = db.get_pending_reminders(24)
    sms_ok = sms_reminder({"service": "Συνεδρία", "time": "10:00", "phone": "6970000001"}, 24)
    step("4. Reminder path λειτουργεί (χωρίς crash)", isinstance(pending, list) and sms_ok is False,
         f"get_pending_reminders→{len(pending)} · sms_reminder→{sms_ok} (False=λείπει TWILIO_PHONE_NUMBER, ΟΧΙ σφάλμα)")
except Exception as e:
    step("4. Reminder path λειτουργεί (χωρίς crash)", False, f"EXCEPTION: {e}")

# 5) Ο Α ΑΚΥΡΩΝΕΙ → ο Β της waitlist τραβιέται → ξαναγεμίζει το slot → revenue σώζεται
db.cancel_booking(a)
waiter = db.pop_first_waiter(S, TOM, "10:00")
b = db.create_booking(S, TOM, "10:00", "Συνεδρία", 30, waiter["name"], waiter["phone"]) if waiter else None
empty_after = db.pop_first_waiter(S, TOM, "10:00")
step("5. Ακύρωση → waitlist γεμίζει τον επόμενο (revenue προστατευμένο)",
     waiter is not None and waiter["name"] == "Ασθενής Β" and bool(b) and empty_after is None,
     f"ο Β πήρε τη θέση (booking #{b}) · waitlist άδεια: {empty_after is None}")

# 6) No-show → outcome καταγράφεται (feed για recovery)
c = db.create_booking(S, TOM, "12:00", "Συνεδρία", 30, "Ασθενής Γ", "6970000003")
db.set_booking_pending_outcome(c)
ns = db.mark_booking_outcome(c, "no_show")
step("6. No-show καταγράφεται (recovery feed)", bool(ns), f"booking #{c} → no_show marked={ns}")

# 7) Restart survival — ξανανοίγει η βάση, τα δεδομένα ΕΠΙΒΙΩΝΟΥΝ
import importlib
importlib.reload(db)
biz2 = db.get_business(S)
appts2 = db.get_bookings_for_date(S, TOM)
step("7. Δεδομένα επιβιώνουν restart",
     bool(biz2) and len(appts2) >= 1,
     f"μετά από reload: ιατρείο ζει, {len(appts2)} ραντεβού στη βάση")

print("\n" + "═" * 62)
if fails:
    print(f"❌ {len(fails)} ΒΗΜΑΤΑ ΣΚΑΣΑΝ: {fails}\n   → ΔΕΝ είναι έτοιμο. Αυτά πρέπει να φτιαχτούν.")
    sys.exit(1)
print("✅ ΜΙΑ ΟΛΟΚΛΗΡΗ ΜΕΡΑ ΙΑΤΡΕΙΟΥ ΕΤΡΕΞΕ ΣΕ ΠΡΑΓΜΑΤΙΚΟ ΚΩΔΙΚΑ ΧΩΡΙΣ ΝΑ ΣΚΑΣΕΙ.")
print("   (Το SMS/voice/email φεύγουν ΜΟΝΟ όταν μπουν τα keys — αυτό είναι switch, όχι bug.)")
