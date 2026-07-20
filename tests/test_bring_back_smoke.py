"""Ground-truth test: 'φέρνει πίσω πελάτες' (win-back + no-show recovery).
Temp DB. ΧΩΡΙΣ πραγματικά sends (monkeypatch Resend/Twilio) — αποδεικνύει τη ροή + το κενό."""
import os, sys, tempfile
from datetime import datetime, timedelta

os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "rt_bringback_test.db")
if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
from voice import outbound, twilio_client

db.init_db()
SLUG = "bb-clinic"
db.create_business(SLUG, {"name": "Κλινική BB", "type": "φυσικοθεραπευτήριο", "slot_minutes": 30})

fails = []
def check(label, ok):
    print(f"  {'✅' if ok else '❌'} {label}")
    if not ok: fails.append(label)

# capture αντί για πραγματικό send
sent = {"email": [], "sms": [], "owner_alert": []}
import email_service
email_service.send_email_via_resend = lambda to, subj, html, **k: (sent["email"].append((to, subj)) or True)
email_service.send_owner_notification = lambda slug, ev: (sent["owner_alert"].append(ev.get("type")) or True)
twilio_client.is_configured = lambda: False          # Twilio OFF (όπως στην παραγωγή σήμερα)
outbound.twilio_client.is_configured = lambda: False
outbound.vapi_client.is_configured = lambda: False    # Vapi-attach OFF (no Twilio number) → voice νεκρό
outbound._within_contact_hours = lambda *a, **k: True  # παρακάμπτουμε quiet-hours για ντετερμινισμό

print("\n=== WIN-BACK: ανενεργός 120 ημ → win_back job ===")
old = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
bid = db.create_booking(SLUG, old, "10:00", "Συνεδρία", 30, "Παλιός Ασθενής", "69711100001", "old@x.gr")
with db._conn() as c:
    c.execute("UPDATE bookings SET status='completed' WHERE id=?", (bid,)); c.commit()
n = outbound.enqueue_winback_for_inactive_customers(SLUG, days_inactive=90, limit=20)
check(f"enqueue_winback δημιούργησε {n} job(s)", n == 1)
with db._conn() as c:
    wb = c.execute("SELECT job_type, channel FROM outbound_jobs WHERE slug=? AND job_type='win_back'", (SLUG,)).fetchone()
check("win_back job υπάρχει (channel=voice)", wb and wb["channel"] == "voice")
n2 = outbound.enqueue_winback_for_inactive_customers(SLUG, days_inactive=90, limit=20)
check(f"cooldown: 2η φορά δεν ξανα-enqueue ({n2})", n2 == 0)

print("\n=== NO-SHOW RECOVERY: παράδοση μηνύματος ===")
# job με email → email fallback πρέπει να φύγει
db.enqueue_outbound_job(slug=SLUG, job_type="no_show_recovery",
    customer_phone="69711100002", customer_name="Με Email", customer_email="patient@x.gr",
    booking_id=None, scheduled_at=(datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
    channel="sms", payload={"rebook_url": "https://www.ran-tevu.gr/bb-clinic"})
# job ΧΩΡΙΣ email (μόνο τηλέφωνο) → το κενό
db.enqueue_outbound_job(slug=SLUG, job_type="no_show_recovery",
    customer_phone="69711100003", customer_name="Μόνο Τηλέφωνο", customer_email="",
    booking_id=None, scheduled_at=(datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
    channel="sms", payload={"rebook_url": "https://www.ran-tevu.gr/bb-clinic"})

stats = outbound.process_due_jobs(max_jobs=10)
print("  stats:", stats)
check("ασθενής ΜΕ email: recovery έφυγε με email (fallback) ✅", len(sent["email"]) == 1)
check("το email ήταν no-show recovery", sent["email"] and "ξανακλείσουμε" in sent["email"][0][1])

# επιβεβαίωση του κενού: ο μόνο-τηλέφωνο ασθενής ΔΕΝ παραδόθηκε
with db._conn() as c:
    phone_only = c.execute("""SELECT status, result FROM outbound_jobs
                              WHERE slug=? AND customer_phone='69711100003'""", (SLUG,)).fetchone()
print(f"  μόνο-τηλέφωνο job → status={phone_only['status']}, result={phone_only['result']}")
check("FIX #3: μόνο-τηλέφωνο → failed (όχι ατέρμονο retry) + no_reachable_channel",
      phone_only["status"] == "failed" and "no_reachable_channel" in (phone_only["result"] or ""))
check("FIX #3: ο ιδιοκτήτης ΕΙΔΟΠΟΙΗΘΗΚΕ (όχι σιωπηλή απώλεια)",
      "outbound_unreachable" in sent["owner_alert"])

print("\n=== FIX #2: win-back voice νεκρό → email fallback ===")
sent["email"].clear()
db.enqueue_outbound_job(slug=SLUG, job_type="win_back",
    customer_phone="69711100004", customer_name="Παλιός ΜΕ Email", customer_email="wb@x.gr",
    booking_id=None, scheduled_at=(datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
    channel="voice", payload={"rebook_url": "https://www.ran-tevu.gr/bb-clinic"})
outbound.process_due_jobs(max_jobs=10)
check("win-back έφυγε με email (voice→email fallback)", len(sent["email"]) == 1)
check("το email ήταν win-back copy (15% / σας θυμηθήκαμε)",
      sent["email"] and "θυμηθήκαμε" in sent["email"][0][1])

print("\n=== EXECUTOR επίπεδο ===")
job_email = {"slug": SLUG, "job_type": "no_show_recovery", "customer_phone": "6971110000X",
             "customer_name": "Δοκιμή", "customer_email": "x@x.gr", "payload_json": '{"rebook_url":"u"}'}
ok, msg = outbound._execute_email_job(job_email)
check(f"_execute_email_job → email_sent ({msg})", ok and msg == "email_sent")
job_sms = {"slug": SLUG, "job_type": "no_show_recovery", "customer_phone": "p",
           "customer_name": "x", "customer_email": "", "payload_json": "{}"}
ok2, msg2 = outbound._execute_sms_job(job_sms)
check(f"_execute_sms_job με Twilio OFF → twilio_not_configured ({msg2})", not ok2 and msg2 == "twilio_not_configured")

print("\n" + "=" * 54)
if fails:
    print(f"❌ {len(fails)} FAILURES: {fails}"); sys.exit(1)
print("✅ ΟΛΑ ΠΕΡΑΣΑΝ — winback+recovery δουλεύουν· το κενό (μόνο-τηλέφωνο χωρίς SMS) επιβεβαιώθηκε.")
