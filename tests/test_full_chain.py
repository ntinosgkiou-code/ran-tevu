"""MASTER end-to-end chain test: αποδεικνύει ότι ΟΛΑ συνδέονται & ο Γραμματέας εκτελεί
ΚΑΘΕ ενέργεια + απαντά ΚΑΘΕ query. Temp DB, χωρίς πραγματικά sends/LLM (δοκιμάζει το
execution layer απευθείας — η LLM→tool χαρτογράφηση έχει επιβεβαιωθεί live ξεχωριστά)."""
import os, sys, tempfile, io
from datetime import datetime, timedelta

os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "rt_fullchain.db")
if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])
from cryptography.fernet import Fernet
os.environ["RANTEVU_ENC_KEY"] = Fernet.generate_key().decode()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
import back_office as bo
import back_office_assistant as A
from voice import outbound, twilio_client
import email_service
import routes.billing_routes as B
from routes import back_office_routes as R

# capture αντί real sends
sent = {"email": [], "owner": []}
email_service.send_email_via_resend = lambda *a, **k: (sent["email"].append(a[:2]) or True)
email_service.send_owner_notification = lambda s, e: (sent["owner"].append(e.get("type")) or True)
twilio_client.is_configured = lambda: False
outbound.twilio_client.is_configured = lambda: False
outbound.vapi_client.is_configured = lambda: False
outbound._within_contact_hours = lambda *a, **k: True

db.init_db(); bo.init_back_office(); B.ensure_table()
SLUG = "chain"
fails = []
def chk(label, ok):
    print(f"  {'✅' if ok else '❌'} {label}")
    if not ok: fails.append(label)

print("\n═══ 1. PROVISION + ONBOARDING ═══")
db.create_business(SLUG, {"name": "Φυσικο Chain", "type": "φυσικοθεραπευτήριο", "slot_minutes": 30, "services": []})
# onboarding save (ίδια λογική με bo_setup_save)
db.update_business(SLUG, {"day_start": "09:00", "day_end": "20:00", "slot_minutes": 30,
                          "closed_days": [6], "services": [{"name": "Συνεδρία", "price": 40, "duration": 30}],
                          "hours": {"Κυριακή": "Κλειστά"}})
biz = db.get_business(SLUG)
chk("onboarding: services+hours saved", biz["services"][0]["price"] == 40 and biz["day_start"] == "09:00")

print("\n═══ 2. AI ΓΡΑΜΜΑΤΕΑΣ — ΕΚΤΕΛΕΙ ΚΑΘΕ WRITE ACTION ═══")
tom = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
r = A.execute_action(SLUG, "book_appointment", {"name": "Γιώργος", "phone": "6970001", "date": tom, "time": "10:00"})
chk(f"book_appointment ({r.get('msg') or r.get('error')})", r.get("ok"))
fp = A._run_read_tool(SLUG, "find_patient", {"query": "Γιώργος"})
bid = fp["matches"][0]["appointments"][0]["booking_id"]
r = A.execute_action(SLUG, "reschedule_appointment", {"booking_id": bid, "phone": "6970001", "new_date": tom, "new_time": "12:00"})
chk(f"reschedule_appointment ({r.get('msg') or r.get('error')})", r.get("ok"))
r = A.execute_action(SLUG, "create_invoice", {"phone": "6970001", "name": "Γιώργος", "service": "Συνεδρία", "amount": 40})
chk(f"create_invoice ({r.get('msg') or r.get('error')})", r.get("ok"))
unpaid = A._run_read_tool(SLUG, "list_unpaid_invoices", {})
iid = unpaid["unpaid_invoices"][0]["invoice_id"]
r = A.execute_action(SLUG, "record_payment", {"invoice_id": iid})
chk(f"record_payment ({r.get('msg') or r.get('error')})", r.get("ok"))
r = A.execute_action(SLUG, "add_patient", {"name": "Μαρία", "phone": "6970002", "email": "m@x.gr"})
chk(f"add_patient ({r.get('msg') or r.get('error')})", r.get("ok"))
r = A.execute_action(SLUG, "add_note", {"phone": "6970002", "title": "Πλάνο", "body": "8 συνεδρίες"})
chk(f"add_note ({r.get('msg') or r.get('error')})", r.get("ok"))
r = A.execute_action(SLUG, "send_message", {"phone": "6970002", "name": "Μαρία", "text": "Καλημέρα"})
chk(f"send_message ({r.get('msg') or r.get('error')})", r.get("ok"))
r = A.execute_action(SLUG, "winback", {"days": 90, "limit": 5})
chk(f"winback ({r.get('msg') or r.get('error')})", r.get("ok"))
r = A.execute_action(SLUG, "cancel_appointment", {"booking_id": bid})
chk(f"cancel_appointment ({r.get('msg') or r.get('error')})", r.get("ok"))
r = A.execute_action(SLUG, "drop_table", {})
chk("unknown action μπλοκάρεται (whitelist)", not r.get("ok"))

print("\n═══ 3. AI ΓΡΑΜΜΑΤΕΑΣ — ΑΠΑΝΤΑ ΚΑΘΕ QUERY (chat-first) ═══")
for tool, inp in [("schedule_for_date", {}), ("overview", {}), ("list_debtors", {}),
                  ("low_stock", {}), ("open_tasks", {}), ("check_availability", {"date": tom}),
                  ("list_unpaid_invoices", {}), ("patient_history", {"query": "Μαρία"}),
                  ("recovered_revenue", {}), ("find_patient", {"query": "6970002"})]:
    res = A._run_read_tool(SLUG, tool, inp)
    chk(f"query {tool} → επιστρέφει χωρίς error", isinstance(res, dict) and "error" not in res)

print("\n═══ 4. RECOVERY → LEDGER (attribution) ═══")
# no-show patient → recovery job (2d πριν) → rebooking (μετά) → recovered
with db._conn() as c:
    ts = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO outbound_jobs (slug,job_type,channel,customer_phone,status,scheduled_at,created,result) VALUES (?,?,?,?,?,?,?,?)",
              (SLUG, "no_show_recovery", "email", "6970003", "done", ts, ts, "x")); c.commit()
db.create_booking(SLUG, (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d"), "11:00", "Συνεδρία", 30, "Recov", "6970003")
led = db.recovered_revenue(SLUG, days=90)
chk(f"ledger attributes rebooking (€{led['recovered_eur']}, {led['recovered_count']})", led["recovered_count"] == 1 and led["recovered_eur"] == 40.0)
roi = db.roi_summary(SLUG, days=30)
chk("roi_summary.recovered_eur = attributed", roi["recovered_eur"] == 40.0)

print("\n═══ 5. TIER GATING (premium/standard/demo) ═══")
chk("demo/no-sub → is_premium=True (full access)", B.is_premium(SLUG) is True)
B._upsert_subscription("sub1", slug=SLUG, status="trialing", tier="standard")
chk("standard → is_premium=False (gated)", B.is_premium(SLUG) is False)
chk("_premium_ok: AI Γραμματέας είναι core feature σε ΚΑΘΕ tier (owner-token gate only)", R._premium_ok(SLUG) is True)
B._upsert_subscription("sub1", slug=SLUG, tier="premium")
chk("premium → is_premium=True", B.is_premium(SLUG) is True)

print("\n═══ 6. RECOVERY DELIVERY (channel fallbacks) ═══")
# CONSENT (Ν.3471/2006): η αυτόνομη ανάκτηση στέλνει ΜΟΝΟ σε ασθενή με ρητή συναίνεση 'reminders'
db.log_consent(consent_type="reminders", slug=SLUG, identifier="6970002", accepted=True)
db.enqueue_outbound_job(slug=SLUG, job_type="no_show_recovery", customer_phone="6970002",
    customer_name="Μαρία", customer_email="m@x.gr", booking_id=None,
    scheduled_at=(datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
    channel="sms", payload={"rebook_url": "u"})
sent["email"].clear()
outbound.process_due_jobs(max_jobs=10)
chk("sms→email fallback (Twilio off, έχει email, consent-gated)", len(sent["email"]) == 1)
# ΚΑΙ: χωρίς consent → skip (δεν φεύγει τίποτα)
db.enqueue_outbound_job(slug=SLUG, job_type="no_show_recovery", customer_phone="6970999",
    customer_name="Άγνωστος", customer_email="x@x.gr", booking_id=None,
    scheduled_at=(datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
    channel="sms", payload={"rebook_url": "u"})
sent["email"].clear()
outbound.process_due_jobs(max_jobs=10)
chk("χωρίς consent → recovery skipped (no auto-blast)", len(sent["email"]) == 0)

print("\n═══ 7. IMPORT PARSER (xlsx) ═══")
from openpyxl import Workbook
wb = Workbook(); ws = wb.active
ws.append(["Όνομα", "Τηλέφωνο"]); ws.append(["Νίκος", "6970004"])
buf = io.BytesIO(); wb.save(buf); buf.seek(0)
class FF:
    filename = "p.xlsx"
    def __init__(self, d): self._d = d
    def read(self): return self._d
rows = R._parse_patient_file(FF(buf.read()))
chk("xlsx import parser", len(rows) == 1 and str(rows[0]["phone"]) == "6970004")

print("\n" + "═" * 56)
if fails:
    print(f"❌ {len(fails)} FAILURES: {fails}"); sys.exit(1)
print("✅ FULL CHAIN OK — όλα συνδέονται, ο Γραμματέας εκτελεί ΚΑΘΕ ενέργεια + απαντά ΚΑΘΕ query.")
