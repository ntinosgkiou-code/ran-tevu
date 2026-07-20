"""ΑΠΟΔΕΙΞΗ: το Ran-Tevu risk layer flag-άρει τον σωστό ασθενή με ΛΟΓΟΥΣ, και το escalated
reminder στέλνεται ΜΙΑ φορά (idempotent) στους high-risk — σε πραγματική βάση, πραγματικός κώδικας."""
import os, sys, tempfile
from datetime import datetime, timedelta

os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "rt_risk_proof.db")
if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])
from cryptography.fernet import Fernet
os.environ["RANTEVU_ENC_KEY"] = Fernet.generate_key().decode()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
from risk_brain import scan_upcoming

db.init_db()
S = "iatreio"
db.create_business(S, {"name": "Ιατρείο Δίας", "type": "ιατρείο", "slot_minutes": 30,
                       "day_start": "09:00", "day_end": "20:00",
                       "services": [{"name": "Εξέταση", "price": 50, "duration": 30}]})
D = lambda n: (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")
fails = []
def show(step, proof, ok):
    print(f"  {'✅' if ok else '❌'} {step}\n       → {proof}")
    if not ok: fails.append(step)

print("\n🧠 1) Ιστορικό: ο Χρόνιος έχει 2/3 no-show, ο Συνεπής 3/3 ήρθε")
# παρελθόν: γράφουμε ΑΠΕΥΘΕΙΑΣ (το create_booking σωστά αρνείται παρελθοντικές ημερομηνίες)
with db._conn() as c:
    for dd, st in [(-30, "completed"), (-20, "no_show"), (-10, "no_show")]:
        c.execute("INSERT INTO bookings (slug,date,time,service,duration,name,phone,status) VALUES (?,?,?,?,?,?,?,?)",
                  (S, (datetime.now() + timedelta(days=dd)).strftime("%Y-%m-%d"), "10:00", "Εξέταση", 30, "Χρόνιος", "6970000001", st))
    for dd in (-25, -15, -5):
        c.execute("INSERT INTO bookings (slug,date,time,service,duration,name,phone,status) VALUES (?,?,?,?,?,?,?,?)",
                  (S, (datetime.now() + timedelta(days=dd)).strftime("%Y-%m-%d"), "11:00", "Εξέταση", 30, "Συνεπής", "6970000002", "completed"))
    c.commit()
b1 = db.create_booking(S, D(2), "10:00", "Εξέταση", 30, "Χρόνιος", "6970000001")
b2 = db.create_booking(S, D(2), "11:00", "Εξέταση", 30, "Συνεπής", "6970000002")
show("τα επερχόμενα κλείστηκαν", f"bookings {b1},{b2} για {D(2)}", bool(b1 and b2))

print("\n⚠️ 2) Το risk scan βρίσκει τον σωστό — με τον λόγο")
scan = scan_upcoming(S, horizon_days=7)
top = scan["appointments"][0] if scan["appointments"] else {}
show("ο Χρόνιος flag-άρεται high με λόγο",
     f"1ος={top.get('name')} band={top.get('band')} risk={top.get('risk')} λόγοι={top.get('reasons')}",
     top.get("name") == "Χρόνιος" and top.get("band") == "high" and any("no-show" in r for r in top.get("reasons", [])))
sy = next((s for s in scan["appointments"] if s["name"] == "Συνεπής"), {})
show("ο Συνεπής είναι low", f"band={sy.get('band')} (risk {sy.get('risk')})", sy.get("band") == "low")

print("\n📲 3) Escalated reminder: idempotent mark (ακριβώς μία φορά)")
first = db.mark_risk_reminder_sent(b1)
second = db.mark_risk_reminder_sent(b1)
show("1ο mark=True, 2ο mark=False", f"first={first}, second={second}", first is True and second is False)

print("\n🕒 4) Ο scheduler-pass τρέχει χωρίς crash (χωρίς SMS keys → δεν σκάει)")
import app as rt_app
try:
    rt_app.check_risk_reminders()
    ok = True
except Exception as e:
    ok = False
    print("   error:", e)
show("check_risk_reminders() πέρασε", "έτρεξε πάνω σε πραγματική βάση χωρίς exception", ok)

print("\n" + "═" * 60)
if fails:
    print(f"❌ {len(fails)} ΑΠΕΤΥΧΑΝ: {fails}"); sys.exit(1)
print("✅ ΑΠΟΔΕΙΞΗ: risk layer ζωντανό στο Ran-Tevu — σωστό flag, με λόγους, escalated reminder ακριβώς μία φορά.")
