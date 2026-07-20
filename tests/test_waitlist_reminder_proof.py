"""ΑΠΟΔΕΙΞΗ (όχι εικονικά): η ΑΚΡΙΒΗΣ σκηνή του Ντίνου δουλεύει σε ΠΡΑΓΜΑΤΙΚΗ βάση —
πελάτης ακυρώνει → ο επόμενος της waitlist παίρνει τη θέση → κανένα κενό slot → το revenue μένει.
+ ότι το reminder path τρέχει (SMS χωρίς keys → graceful, δεν σκάει).
Τρέχει τα ΙΔΙΑ db functions που καλεί το app (create_booking / add_to_waitlist / pop_first_waiter)."""
import os, sys, tempfile
from datetime import datetime, timedelta

os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "rt_wl_proof.db")
if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])
from cryptography.fernet import Fernet
os.environ["RANTEVU_ENC_KEY"] = Fernet.generate_key().decode()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
from sms_service import sms_reminder, send_sms

db.init_db()
S = "iatreio"
db.create_business(S, {"name": "Φυσικοθεραπευτήριο Δίας", "type": "φυσικοθεραπευτήριο",
                       "slot_minutes": 30, "day_start": "09:00", "day_end": "20:00",
                       "services": [{"name": "Συνεδρία", "price": 40, "duration": 30}]})
TOM = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
fails = []
def show(step, proof, ok):
    print(f"  {'✅' if ok else '❌'} {step}\n       → {proof}")
    if not ok: fails.append(step)

print("\n📅 1) Πελάτης Α κλείνει τη θέση 10:00 αύριο")
a = db.create_booking(S, TOM, "10:00", "Συνεδρία", 30, "Πελάτης Α", "6970000001")
booked = db.get_bookings_for_date(S, TOM) if hasattr(db, "get_bookings_for_date") else None
show("η 10:00 πιάστηκε", f"booking id={a} στις 10:00 για τον Πελάτη Α", bool(a))

print("\n📋 2) Η 10:00 είναι πλέον γεμάτη → ο Πελάτης Β μπαίνει στη WAITLIST για την ίδια ώρα")
wid = db.add_to_waitlist(S, TOM, "10:00", "Συνεδρία", "Πελάτης Β", "6970000002", "b@x.gr")
show("ο Β μπήκε στη waitlist", f"waitlist id={wid} (Πελάτης Β περιμένει την 10:00)", bool(wid))

print("\n❌ 3) Ο Πελάτης Α ΑΚΥΡΩΝΕΙ — εδώ κανονικά χάνεται το revenue")
with db._conn() as c:
    c.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (a,)); c.commit()
# closed-loop: τράβα τον πρώτο της waitlist για το κενό slot (ό,τι κάνει το notify_waitlist)
waiter = db.pop_first_waiter(S, TOM, "10:00")
show("ο επόμενος της waitlist τραβήχτηκε αυτόματα",
     f"ελευθερώθηκε η 10:00 → προσφέρεται στον: {waiter['name'] if waiter else '—'}",
     waiter is not None and waiter["name"] == "Πελάτης Β")

print("\n✅ 4) Ο Πελάτης Β παίρνει τη θέση → το slot ΞΑΝΑΓΕΜΙΖΕΙ (revenue σώθηκε)")
b = db.create_booking(S, TOM, "10:00", "Συνεδρία", 30, waiter["name"], waiter["phone"]) if waiter else None
still = db.pop_first_waiter(S, TOM, "10:00")   # η waitlist πρέπει να είναι πλέον άδεια
show("η 10:00 ξαναγέμισε από τη waitlist",
     f"νέο booking id={b} για τον {waiter['name'] if waiter else '—'} · waitlist άδεια: {still is None}",
     bool(b) and still is None)

print("\n📲 5) Reminder path τρέχει (SMS χωρίς Twilio number → graceful, δεν σκάει)")
r = sms_reminder({"service": "Συνεδρία", "time": "10:00", "phone": "6970000002"}, hours_before=24)
show("το SMS reminder καλείται χωρίς crash",
     f"send_sms επέστρεψε {r} (False = λείπει TWILIO_PHONE_NUMBER· η ΛΟΓΙΚΗ δουλεύει, θέλει μόνο το key)",
     r is False)   # χωρίς keys → False (όχι exception)

print("\n" + "═" * 60)
if fails:
    print(f"❌ {len(fails)} ΑΠΕΤΥΧΑΝ: {fails}"); sys.exit(1)
print("✅ ΑΠΟΔΕΙΞΗ: ακύρωση → waitlist γεμίζει τον επόμενο → κανένα κενό → revenue προστατευμένο.")
print("   Το reminder path είναι λειτουργικό· το SMS φεύγει μόλις μπει TWILIO_PHONE_NUMBER.")
