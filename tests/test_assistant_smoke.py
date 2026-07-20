"""Functional smoke test για τον AI Γραμματέα + Excel import. Temp DB, χωρίς LLM call
(δοκιμάζει τους READ executors, execute_action, και τον parser απευθείας)."""
import os, sys, tempfile, io

os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "rt_assistant_test.db")
if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
import back_office as bo
import back_office_assistant as A

db.init_db()
bo.init_back_office()

SLUG = "test-clinic"
db.create_business(SLUG, {
    "name": "Φυσικοθεραπευτήριο Τεστ", "type": "φυσικοθεραπευτήριο",
    "slot_minutes": 30, "day_start": "09:00", "day_end": "20:00",
    "services": [{"name": "Συνεδρία φυσικοθεραπείας", "price": 40}],
})

P = lambda label, ok: print(f"  {'✅' if ok else '❌'} {label}")
fails = []
def check(label, ok):
    P(label, ok)
    if not ok: fails.append(label)

print("\n=== EXECUTE: book_appointment ===")
r = A.execute_action(SLUG, "book_appointment",
                     {"name": "Γιώργος Παπάς", "phone": "6970000001", "date": "2026-12-30", "time": "17:00"})
check(f"book_appointment ok ({r.get('msg') or r.get('error')})", r.get("ok"))

print("\n=== READ: find_patient ===")
fp = A._run_read_tool(SLUG, "find_patient", {"query": "Γιώργος"})
m = fp.get("matches", [])
check("find_patient βρήκε τον Γιώργο", len(m) == 1 and m[0]["phone"] == "6970000001")
bid = m[0]["appointments"][0]["booking_id"] if m and m[0]["appointments"] else None
check("find_patient επέστρεψε booking_id", bid is not None)

print("\n=== READ: check_availability ===")
ca = A._run_read_tool(SLUG, "check_availability", {"date": "2026-12-30"})
check(f"check_availability γύρισε free_slots ({len(ca.get('free_slots', []))})", "free_slots" in ca)
check("17:00 ΔΕΝ είναι πια ελεύθερο (κλείστηκε)", "17:00" not in ca.get("free_slots", []))

print("\n=== EXECUTE: double-booking guard ===")
r2 = A.execute_action(SLUG, "book_appointment",
                      {"name": "Άλλος", "phone": "6970000099", "date": "2026-12-30", "time": "17:00"})
check("double-booking μπλοκάρεται", not r2.get("ok") and "πιασμέν" in (r2.get("error") or ""))

print("\n=== EXECUTE: reschedule ===")
r3 = A.execute_action(SLUG, "reschedule_appointment",
                      {"booking_id": bid, "phone": "6970000001", "new_date": "2026-12-31", "new_time": "11:00"})
check(f"reschedule ok ({r3.get('msg') or r3.get('error')})", r3.get("ok"))

print("\n=== EXECUTE: create_invoice + record_payment ===")
ri = A.execute_action(SLUG, "create_invoice",
                      {"phone": "6970000001", "name": "Γιώργος Παπάς", "service": "Συνεδρία", "amount": 40})
check(f"create_invoice ok ({ri.get('msg') or ri.get('error')})", ri.get("ok"))
unpaid = A._run_read_tool(SLUG, "list_unpaid_invoices", {})
inv_id = unpaid["unpaid_invoices"][0]["invoice_id"] if unpaid.get("unpaid_invoices") else None
check("list_unpaid_invoices βρήκε το τιμολόγιο", inv_id is not None)
rp = A.execute_action(SLUG, "record_payment", {"invoice_id": inv_id})
check(f"record_payment (εξόφληση υπολοίπου) ok ({rp.get('msg') or rp.get('error')})", rp.get("ok"))
unpaid2 = A._run_read_tool(SLUG, "list_unpaid_invoices", {})
check("μετά την πληρωμή δεν υπάρχει ανεξόφλητο", len(unpaid2.get("unpaid_invoices", [])) == 0)

print("\n=== EXECUTE: add_patient + add_note + cancel ===")
ra = A.execute_action(SLUG, "add_patient", {"name": "Μαρία Ι.", "phone": "6970000002", "email": "m@x.gr"})
check("add_patient ok", ra.get("ok"))
rn = A.execute_action(SLUG, "add_note", {"phone": "6970000002", "title": "Πλάνο", "body": "8 συνεδρίες"})
check("add_note ok", rn.get("ok"))
rc = A.execute_action(SLUG, "cancel_appointment", {"booking_id": bid})
check(f"cancel_appointment ok ({rc.get('msg') or rc.get('error')})", rc.get("ok"))

print("\n=== SECURITY: μη-whitelisted tool απορρίπτεται ===")
rx = A.execute_action(SLUG, "drop_table", {})
check("άγνωστο tool μπλοκάρεται", not rx.get("ok"))

print("\n=== IMPORT PARSER: xlsx + csv ===")
from openpyxl import Workbook
wb = Workbook(); ws = wb.active
ws.append(["Όνομα", "Τηλέφωνο", "Email"])
ws.append(["Κώστας Δ.", "6971111111", "k@x.gr"])
ws.append(["Ελένη Μ.", 6972222222, ""])
buf = io.BytesIO(); wb.save(buf); buf.seek(0)

# import route's parser ζει στο back_office_routes — προσομοίωσε file-like
from routes import back_office_routes as R
class FakeFile:
    def __init__(self, name, data): self.filename = name; self._d = data
    def read(self): return self._d
rows = R._parse_patient_file(FakeFile("patients.xlsx", buf.read()))
check(f"xlsx parser: 2 γραμμές ({len(rows)})", len(rows) == 2 and str(rows[1]["phone"]) == "6972222222")

csv_data = "name,phone,email\nΝίκος,6973333333,n@x.gr\n".encode("utf-8")
rows2 = R._parse_patient_file(FakeFile("p.csv", csv_data))
check(f"csv parser: 1 γραμμή ({len(rows2)})", len(rows2) == 1 and rows2[0]["phone"] == "6973333333")

print("\n" + "=" * 50)
if fails:
    print(f"❌ {len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("✅ ΟΛΑ ΠΕΡΑΣΑΝ — ο AI Γραμματέας + το Excel import δουλεύουν end-to-end.")
