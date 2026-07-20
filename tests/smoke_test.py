"""
smoke_test.py — End-to-end smoke tests για ολόκληρο το σύστημα.

Τρέχει σε temp DB (δεν αγγίζει production). Ελέγχει: verticals, ΦΠΑ, κρυπτογράφηση,
back-office agents, provisioning, owner-gate, graph links. Run: python tests/smoke_test.py
Exit 0 = όλα πέρασαν, 1 = κάποιο falle.
"""
import os, sys, io, tempfile
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Temp DB + deterministic env ΠΡΙΝ τα imports
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "smoke.db")
os.environ.setdefault("SECRET_KEY", "smoke-secret")
os.environ.setdefault("ADMIN_TOKEN", "smoke-admin")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PASS, FAIL = 0, 0
def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✓ {name}")
    else:
        FAIL += 1; print(f"  ✗ {name}  {detail}")

def section(t): print(f"\n── {t} " + "─" * (46 - len(t)))


# ════════ 1) VERTICAL TEMPLATES + ΦΠΑ ════════
section("Verticals & ΦΠΑ")
import vertical_templates as vt
verts = [k for k, _, _ in vt.list_verticals()]
ok("12 verticals defined", len(verts) == 12, str(verts))
for k in ("physio", "dental", "dermatology", "gp", "cardiology", "gynecology",
          "pediatrics", "orthopedics", "ophthalmology", "medical", "beauty", "fitness"):
    ok(f"vertical '{k}' exists", k in verts)
ok("ιατρικά απαλλαγή ΦΠΑ (dental=0%)", vt.vat_rate_for("οδοντιατρείο") == 0.0)
ok("physio απαλλαγή ΦΠΑ", vt.vat_rate_for("φυσιοθεραπευτήριο") == 0.0)
ok("beauty ΦΠΑ 24%", vt.vat_rate_for("κέντρο αισθητικής") == 24.0)
ok("dermatology recall 365d", vt.winback_days("δερματολογικό ιατρείο") == 365)
ok("pediatrics recall 60d", vt.winback_days("παιδιατρικό ιατρείο") == 60)
ok("resolve alias 'οδοντ'→dental", vt.resolve_vertical("οδοντίατρος κέντρο") == "dental")
vb = vt.voice_block("cardiology")
ok("voice_block has greeting", bool(vb.get("greeting")))
ok("voice_block has emergency_rule", bool(vb.get("emergency_rule")))
ok("every vertical has services+voice", all(
    VERTICAL_TEMPLATES_check := (VT := vt.VERTICAL_TEMPLATES) and
    all(v.get("services") and v.get("voice") for v in VT.values()) for _ in [0]))


# ════════ 2) ΚΡΥΠΤΟΓΡΑΦΗΣΗ ════════
section("Encryption at rest")
from crypto_util import encrypt, decrypt, is_encrypted
secret = "Διάγνωση: μελάνωμα ύποπτο, βιοψία"
enc = encrypt(secret)
ok("encrypt → ciphertext", is_encrypted(enc) and enc != secret)
ok("decrypt round-trip", decrypt(enc) == secret)
ok("idempotent encrypt", encrypt(enc) == enc)
ok("legacy plaintext passthrough", decrypt("παλιό plaintext") == "παλιό plaintext")
ok("empty/None safe", encrypt("") == "" and encrypt(None) is None and decrypt("") == "")


# ════════ 3) DB + BACK-OFFICE CORE ════════
section("Back-office core")
import database as db; db.init_db()
import back_office as bo; bo.init_back_office()
SL = "smoke-clinic"
db.create_business(SL, {"name": "Smoke ΩΡΛ", "type": "οφθαλμιατρείο",
    "services": vt.get_template("ophthalmology")["services"], "day_start": "09:00", "day_end": "21:00"})
ok("business created", db.get_business(SL) is not None)

ph = bo.upsert_patient(SL, "6991234567", "Τεστ Ασθενής", medical_alerts="Αλλεργία ιωδίου")
ok("patient upserted", ph == "6991234567")
with db._conn() as c:
    rawma = c.execute("SELECT medical_alerts FROM bo_patients WHERE slug=? AND phone=?", (SL, ph)).fetchone()[0]
ok("medical_alerts encrypted in DB", is_encrypted(rawma))

nid = bo.add_patient_note(SL, ph, "Διάγνωση: γλαύκωμα αρχικό", kind="clinical", title="Πλάνο")
with db._conn() as c:
    rawnote = c.execute("SELECT body FROM bo_patient_notes WHERE id=?", (nid,)).fetchone()[0]
ok("clinical note encrypted in DB", is_encrypted(rawnote))

iid = bo.create_invoice(SL, ph, "Τεστ Ασθενής", "Έλεγχος", 50, vat_rate=vt.vat_rate_for("οφθαλμιατρείο"))
with db._conn() as c:
    inv = c.execute("SELECT vat_rate, vat, total, mydata_mark FROM bo_invoices WHERE id=?", (iid,)).fetchone()
ok("invoice ΦΠΑ=0 (ιατρικό)", inv[0] == 0 and inv[1] == 0)
ok("invoice total = amount", inv[2] == 50)
ok("invoice ένδειξη απαλλαγής", "Άρθρο 22" in (inv[3] or ""))
bo.record_payment(SL, iid, 50)
with db._conn() as c:
    st = c.execute("SELECT status FROM bo_invoices WHERE id=?", (iid,)).fetchone()[0]
ok("payment → status paid", st == "paid")

p360 = bo.patient_360(SL, ph)
ok("patient_360 decrypts note", p360["notes"][0]["body"] == "Διάγνωση: γλαύκωμα αρχικό")
ok("patient_360 decrypts alerts", p360["profile"]["medical_alerts"] == "Αλλεργία ιωδίου")
ok("patient_360 graph links exist", len(p360["graph"]) >= 1)

bo.add_supplier(SL, "MediShop", "Αναλώσιμα")
bo.add_inventory_item(SL, "Γάντια", qty=1, reorder_level=5)
ok("low_stock detects shortage", len(bo.low_stock(SL)) >= 1)


# ════════ 4) AGENTS REGISTRY ════════
section("Agents")
from back_office_agents import back_office_agents_summary
summ = back_office_agents_summary(SL)
ok("7 back-office agents", summ["total_agents"] == 7, str(summ["total_agents"]))
ok("all agents active", summ["active"] == 7)
from clinic_agents import get_agents_status
ok("9 front-desk agents", len(get_agents_status(SL)) == 9)


# ════════ 5) PROVISIONING (end-to-end) ════════
section("Self-serve provisioning")
import provisioning
res = provisioning.provision("Smoke Δερματολόγος", "dermatology", demo=True)
ok("provision returns slug", bool(res.get("slug")))
ok("provision creates patients", res["summary"]["patients"] > 20, str(res["summary"]["patients"]))
ok("provision creates invoices", res["summary"]["invoices"] > 10)
ok("provision creates graph links", res["summary"]["graph_links"] > 50)
ok("provision links (chat/dash/bo)", all(k in res["links"] for k in ("chat", "dashboard", "back_office")))
# ΦΠΑ στα provisioned invoices = 0 (ιατρικό)
with db._conn() as c:
    vr = c.execute("SELECT DISTINCT vat_rate FROM bo_invoices WHERE slug=?", (res["slug"],)).fetchall()
ok("provisioned invoices ΦΠΑ=0", all(r[0] == 0 for r in vr), str([r[0] for r in vr]))


# ════════ 6) OWNER GATE ════════
section("Owner gate (security)")
os.environ["OWNER_PASSWORD"] = "smoke-owner-pw"
import importlib
import routes.start_routes as sr
importlib.reload(sr)
tok1 = sr._owner_token(); tok2 = sr._owner_token()
ok("owner token deterministic", tok1 == tok2 and len(tok1) == 32)
ok("owner password from env", sr._owner_password() == "smoke-owner-pw")
# χωρίς cookie → όχι owner (app context)
from flask import Flask
_app = Flask(__name__); _app.register_blueprint(sr.bp)
with _app.test_request_context("/start"):
    ok("no cookie → not owner", sr.is_owner() is False)
with _app.test_request_context("/start", headers={"Cookie": f"rt_owner={tok1}"}):
    ok("valid cookie → owner", sr.is_owner() is True)
with _app.test_request_context("/start", headers={"Cookie": "rt_owner=wrong"}):
    ok("wrong cookie → not owner", sr.is_owner() is False)


# ════════ 7) OWNER ACTIONS (CRUD endpoints, end-to-end via HTTP) ════════
section("Owner actions (CRUD via HTTP)")
from routes.back_office_routes import bp as bo_bp
from routes.dashboard_routes import dash_token
appx = Flask(__name__)
appx.register_blueprint(bo_bp)
client = appx.test_client()
SL2 = "smoke-actions"
db.create_business(SL2, {"name": "Smoke Οδοντ", "type": "οδοντιατρείο",
                         "services": vt.get_template("dental")["services"]})
tk = dash_token(SL2)
PH = "6900000099"

r = client.post(f"/api/{SL2}/back-office/patient?t={tk}",
                json={"name": "Νέος Α", "phone": PH, "medical_alerts": "αλλεργία πενικιλίνη"})
ok("POST patient → 200 ok", r.status_code == 200 and r.get_json().get("ok"))
with db._conn() as c:
    ma = c.execute("SELECT medical_alerts FROM bo_patients WHERE slug=? AND phone=?", (SL2, PH)).fetchone()[0]
ok("posted alerts encrypted in DB", is_encrypted(ma))

r = client.post(f"/api/{SL2}/back-office/note?t={tk}", json={"phone": PH, "title": "Τ", "body": "διάγνωση Χ"})
ok("POST note → ok", r.get_json().get("ok"))
with db._conn() as c:
    nb = c.execute("SELECT body FROM bo_patient_notes WHERE slug=? ORDER BY id DESC LIMIT 1", (SL2,)).fetchone()[0]
ok("posted note encrypted in DB", is_encrypted(nb))

r = client.post(f"/api/{SL2}/back-office/invoice?t={tk}",
                json={"phone": PH, "name": "Νέος Α", "service": "Καθαρισμός", "amount": 60})
iid = r.get_json().get("id")
ok("POST invoice → ok, ΦΠΑ=0 (ιατρικό)", r.get_json().get("ok") and r.get_json().get("vat_rate") == 0)

r = client.post(f"/api/{SL2}/back-office/payment?t={tk}", json={"invoice_id": iid})
ok("POST payment → ok", r.get_json().get("ok"))
with db._conn() as c:
    st = c.execute("SELECT status FROM bo_invoices WHERE id=?", (iid,)).fetchone()[0]
ok("invoice → status paid", st == "paid")

# PAYMENT EDGE CASES (review HIGH bug fixes)
r = client.post(f"/api/{SL2}/back-office/payment?t={tk}", json={"invoice_id": iid})
ok("διπλή πληρωμή → 400 (ήδη εξοφλημένο)", r.status_code == 400)
with db._conn() as c:
    paid2 = c.execute("SELECT paid, total FROM bo_invoices WHERE id=?", (iid,)).fetchone()
ok("paid δεν ξεπέρασε το total (no overpay)", paid2["paid"] <= paid2["total"] + 0.01)
# νέο τιμολόγιο για negative/overpay test
r = client.post(f"/api/{SL2}/back-office/invoice?t={tk}", json={"phone": PH, "service": "Χ", "amount": 100})
iid2 = r.get_json().get("id")
neg = bo.record_payment(SL2, iid2, -50)
ok("αρνητική πληρωμή → reject", neg.get("ok") is False)
big = bo.record_payment(SL2, iid2, 999)  # overpay
ok("overpay → capped στο remaining", big.get("ok") and big.get("applied") == 100)
with db._conn() as c:
    p3 = c.execute("SELECT paid, total FROM bo_invoices WHERE id=?", (iid2,)).fetchone()
ok("overpay: paid == total ακριβώς", abs(p3["paid"] - p3["total"]) < 0.01)

bo.add_task(SL2, "test task")
with db._conn() as c:
    tid = c.execute("SELECT id FROM bo_tasks WHERE slug=? ORDER BY id DESC LIMIT 1", (SL2,)).fetchone()[0]
r = client.post(f"/api/{SL2}/back-office/task-done?t={tk}", json={"task_id": tid})
ok("POST task-done → ok", r.get_json().get("ok"))
with db._conn() as c:
    ts = c.execute("SELECT status FROM bo_tasks WHERE id=?", (tid,)).fetchone()[0]
ok("task → status done", ts == "done")

# SECURITY: χωρίς token → 403
r = client.post(f"/api/{SL2}/back-office/patient", json={"phone": "6900000098"})
ok("no token → 403 (locked)", r.status_code == 403)
r = client.post(f"/api/{SL2}/back-office/invoice?t=WRONG", json={"phone": PH, "amount": 50})
ok("wrong token → 403 (locked)", r.status_code == 403)
# validation: invoice χωρίς ποσό → 400
r = client.post(f"/api/{SL2}/back-office/invoice?t={tk}", json={"phone": PH, "amount": 0})
ok("invoice amount<=0 → 400", r.status_code == 400)


# ════════ SUMMARY ════════
print("\n" + "═" * 50)
print(f"  RESULT: {PASS} passed, {FAIL} failed  ({PASS+FAIL} total)")
print("═" * 50)
sys.exit(0 if FAIL == 0 else 1)
