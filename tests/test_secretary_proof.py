"""ΑΠΟΔΕΙΞΗ: κάθε λειτουργία της λίστας του Γραμματέα ΕΚΤΕΛΕΙΤΑΙ ΟΝΤΩΣ (όχι απλά λέγεται)
+ πώς ΣΥΝΕΝΝΟΟΥΝΤΑΙ όλα (ραντεβού→τιμολόγιο→οφειλέτης→πληρωμή, no-show→recovery→recovered).
Τρέχει το ΙΔΙΟ layer που καλεί ο Γραμματέας (execute_action / _run_read_tool) σε πραγματική βάση."""
import os, sys, tempfile
from datetime import datetime, timedelta
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "rt_secproof.db")
if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])
from cryptography.fernet import Fernet
os.environ["RANTEVU_ENC_KEY"] = Fernet.generate_key().decode()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db, back_office as bo, back_office_assistant as A
from voice import outbound, twilio_client
import email_service
email_service.send_email_via_resend = lambda *a, **k: True
twilio_client.is_configured = lambda: False
outbound.twilio_client.is_configured = lambda: False
outbound.vapi_client.is_configured = lambda: False
outbound._within_contact_hours = lambda *a, **k: True

db.init_db(); bo.init_back_office()
S = "iatreio"
db.create_business(S, {"name": "Φυσικοθεραπευτήριο Δίας", "type": "φυσικοθεραπευτήριο",
                       "slot_minutes": 30, "day_start": "09:00", "day_end": "20:00",
                       "services": [{"name": "Συνεδρία", "price": 40, "duration": 30}]})
TOM = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
fails = []
def show(cmd, proof, ok):
    print(f"  {'✅' if ok else '❌'} «{cmd}»\n       → {proof}")
    if not ok: fails.append(cmd)

print("\n📅 ΡΑΝΤΕΒΟΥ")
A.execute_action(S, "book_appointment", {"name": "Γιώργος Παπάς", "phone": "6970000001", "date": TOM, "time": "10:00"})
sch = A._run_read_tool(S, "schedule_for_date", {"date": TOM})
show("κλείσε ραντεβού στον Γιώργο αύριο 10", f"το πρόγραμμα δείχνει {sch['count']} ραντεβού: {sch['appointments'][0]['time']} {sch['appointments'][0]['name']}", sch["count"] == 1)
av = A._run_read_tool(S, "check_availability", {"date": TOM})
show("τι ώρες είναι ελεύθερες αύριο", f"{len(av['free_slots'])} slots, η 10:00 ΛΕΙΠΕΙ (πιάστηκε)", "10:00" not in av["free_slots"])
bid = A._run_read_tool(S, "find_patient", {"query": "Γιώργος"})["matches"][0]["appointments"][0]["booking_id"]
A.execute_action(S, "reschedule_appointment", {"booking_id": bid, "phone": "6970000001", "new_date": TOM, "new_time": "12:30"})
sch2 = A._run_read_tool(S, "schedule_for_date", {"date": TOM})
show("μετάθεσε το ραντεβού του Γιώργου στις 12:30", f"το πρόγραμμα δείχνει νέα ώρα: {sch2['appointments'][0]['time']}", sch2["appointments"][0]["time"] == "12:30")

print("\n👤 ΑΣΘΕΝΕΙΣ")
A.execute_action(S, "add_patient", {"name": "Μαρία Κ.", "phone": "6970000002", "email": "maria@x.gr"})
fp = A._run_read_tool(S, "find_patient", {"query": "Μαρία"})
show("πρόσθεσε ασθενή Μαρία Κ.", f"βρέθηκε στον φάκελο: {fp['matches'][0]['name']} ({fp['matches'][0]['phone']})", fp["count"] == 1)
A.execute_action(S, "add_note", {"phone": "6970000002", "title": "Αξιολόγηση", "body": "Οσφυαλγία, πλάνο 8 συνεδρίες"})
hist = A._run_read_tool(S, "patient_history", {"query": "Μαρία"})
show("γράψε στον φάκελο της Μαρίας: οσφυαλγία 8 συνεδρίες", f"ο φάκελος έχει {len(hist['notes'])} σημείωση: «{hist['notes'][0]['title']}»", len(hist["notes"]) == 1)

print("\n💰 ΟΙΚΟΝΟΜΙΚΑ + 🔗 ΣΥΝΕΝΝΟΗΣΗ (ραντεβού→τιμολόγιο→ΟΦΕΙΛΕΤΗΣ→πληρωμή)")
A.execute_action(S, "create_invoice", {"phone": "6970000002", "name": "Μαρία Κ.", "service": "Συνεδρία", "amount": 40})
deb = A._run_read_tool(S, "list_debtors", {})
show("έκδωσε τιμολόγιο €40 στη Μαρία", f"η Μαρία εμφανίστηκε ΑΥΤΟΜΑΤΑ στους ΟΦΕΙΛΕΤΕΣ: {deb['debtors'][0]['patient_name']} χρωστά €{deb['debtors'][0]['owed']}", len(deb["debtors"]) == 1 and deb["debtors"][0]["owed"] == 40)
bo.rebuild_tasks(S)   # ο agent που συνθέτει το to-do της γραμματείας (recall + χρεωστικά + παραγγελίες)
tk = A._run_read_tool(S, "open_tasks", {})
bal = [t["title"] for t in tk["tasks"] if t["kind"] == "balance"]
show("τι εκκρεμεί σήμερα;", f"το χρεωστικό μπήκε ΑΥΤΟΜΑΤΑ ως εργασία: «{bal[0] if bal else '—'}»", len(bal) >= 1)
inv_id = A._run_read_tool(S, "list_unpaid_invoices", {})["unpaid_invoices"][0]["invoice_id"]
A.execute_action(S, "record_payment", {"invoice_id": inv_id})
deb2 = A._run_read_tool(S, "list_debtors", {})
show("η Μαρία πλήρωσε", f"ΕΦΥΓΕ από τους οφειλέτες (συνεννόηση): οφειλέτες τώρα {len(deb2['debtors'])}", len(deb2["debtors"]) == 0)

print("\n📊 ΕΠΙΣΚΟΠΗΣΗ & 📦 ΑΠΟΘΗΚΗ")
ov = A._run_read_tool(S, "overview", {})
show("πώς πάμε;", f"έσοδα 30ημ €{ov['finance']['revenue']}, κέρδος €{ov['finance']['profit']}, ασθενείς {ov['patients']}", "finance" in ov)
bo.add_inventory_item(S, "Gel υπερήχων", qty=1, reorder_level=3, unit="τεμ")
ls = A._run_read_tool(S, "low_stock", {})
show("τι να παραγγείλω;", f"χαμηλό απόθεμα: {ls['low_stock'][0]['name']} ({ls['low_stock'][0]['qty']}/{ls['low_stock'][0]['reorder_level']})", len(ls["low_stock"]) >= 1)

print("\n📩 ΕΠΙΚΟΙΝΩΝΙΑ")
A.execute_action(S, "send_message", {"phone": "6970000002", "name": "Μαρία", "text": "Υπενθύμιση: αύριο 12:30"})
with db._conn() as c:
    comm = c.execute("SELECT body FROM bo_comm_log WHERE slug=? AND patient_phone='6970000002'", (S,)).fetchone()
show("στείλε υπενθύμιση στη Μαρία", f"καταγράφηκε στο ιστορικό επικοινωνίας: «{comm['body']}»", comm is not None)

print("\n🔄 ΑΝΑΚΤΗΣΗ ΑΣΘΕΝΩΝ (το full loop: no-show→recovery→ξανακλείνει→recovered €)")
# ανενεργός 120 μέρες → win-back
old = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
b = db.create_booking(S, old, "10:00", "Συνεδρία", 30, "Παλιός", "6970000003")
with db._conn() as c:
    c.execute("UPDATE bookings SET status='completed' WHERE id=?", (b,)); c.commit()
n = A.execute_action(S, "winback", {"days": 90, "limit": 10})
show("κάνε win-back στους ανενεργούς", n.get("msg"), n.get("ok"))
# no-show → recovery job (2μέρες πριν) → ο ασθενής ξανακλείνει (rebooking) → recovered
ts = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
with db._conn() as c:
    c.execute("INSERT INTO outbound_jobs (slug,job_type,channel,customer_phone,status,scheduled_at,created,result) VALUES (?,?,?,?,?,?,?,?)",
              (S, "no_show_recovery", "email", "6970000004", "done", ts, ts, "x")); c.commit()
db.create_booking(S, (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d"), "09:30", "Συνεδρία", 30, "Επιστροφή", "6970000004")
rec = A._run_read_tool(S, "recovered_revenue", {})
show("πόσα μου έφερες πίσω;", f"ΠΡΑΓΜΑΤΙΚΑ ανακτημένα: €{rec['recovered_eur']} ({rec['recovered_count']} ασθενής ξαναέκλεισε μετά το recovery)", rec["recovered_eur"] == 40.0)

print("\n🔗 ΠΩΣ ΣΥΝΕΝΝΟΟΥΝΤΑΙ ΟΛΑ (knowledge graph ενός ασθενή)")
g = bo.patient_360(S, "6970000002")
links = g.get("graph", [])
show("δείξε μου τη Μαρία (όλα συνδεδεμένα)",
     f"η Μαρία συνδέεται με: {len(g['appointments'])} ραντεβού + {len(g['invoices'])} τιμολόγια + {len(g['notes'])} σημειώσεις + {len(g['communications'])} επικοινωνίες = {len(links)} edges στο graph",
     len(links) >= 3)

print("\n" + "═" * 60)
if fails:
    print(f"❌ {len(fails)} ΔΕΝ εκτελέστηκαν: {fails}"); sys.exit(1)
print("✅ ΚΑΘΕ λειτουργία της λίστας ΕΚΤΕΛΕΣΤΗΚΕ ΟΝΤΩΣ (άλλαξε δεδομένα) + όλα συνεννοούνται.")
