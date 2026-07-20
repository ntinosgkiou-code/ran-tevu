"""SECURITY regression: το query_data ΔΕΝ διαρρέει cross-tenant + προστατεύει ευαίσθητους πίνακες,
ΑΛΛΑ valid analytical queries δουλεύουν (auto-scoped στο ιατρείο). Permanent guard."""
import os, sys, tempfile
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "rt_qsec.db")
if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database as db, back_office_assistant as A

db.init_db()
db.create_business("clinicA", {"name": "A", "type": "physio", "slot_minutes": 30, "services": []})
db.create_business("clinicB", {"name": "B", "type": "physio", "slot_minutes": 30, "services": []})
db.create_booking("clinicA", "2026-07-01", "10:00", "Σ", 30, "ΑσθΑ", "690A", "")
db.create_booking("clinicB", "2026-07-01", "10:00", "Σ", 30, "ΜΥΣΤΙΚΟΣ_B", "690B", "")

fails = []
def chk(label, ok):
    print(f"  {'✅' if ok else '❌'} {label}")
    if not ok: fails.append(label)
qd = lambda slug, s: A._read_query_data(slug, s)
nm = lambda r: [x.get("name") for x in r.get("rows", [])]

print("VALID (γενική ισχύς, auto-scoped):")
chk("count δικών δεδομένων δουλεύει", (qd("clinicA", "SELECT COUNT(*) n FROM bookings WHERE slug='clinicA'").get("rows") or [{}])[0].get("n") == 1)
chk("χωρίς slug filter → auto-scoped μόνο clinicA", nm(qd("clinicA", "SELECT name FROM bookings")) == ["ΑσθΑ"])
chk("CTE δουλεύει", "error" not in qd("clinicA", "WITH d AS (SELECT name FROM bookings) SELECT COUNT(*) n FROM d"))

print("CROSS-TENANT LEAK (πρέπει ΟΝΤΩΣ κλειστό — όχι λόγω error):")
chk("tautology → μόνο clinicA", nm(qd("clinicA", "SELECT name FROM bookings WHERE slug='clinicA' OR 'clinicA'='clinicA'")) == ["ΑσθΑ"])
chk("comment bypass → ΟΧΙ clinicB", "ΜΥΣΤΙΚΟΣ_B" not in nm(qd("clinicA", "SELECT name FROM bookings WHERE slug='clinicB' -- x")))
chk("direct slug=clinicB → ΟΧΙ clinicB", "ΜΥΣΤΙΚΟΣ_B" not in nm(qd("clinicA", "SELECT name FROM bookings WHERE slug='clinicB'")))
chk("UNION-ish / OR 1=1 → μόνο clinicA", "ΜΥΣΤΙΚΟΣ_B" not in nm(qd("clinicA", "SELECT name FROM bookings WHERE 1=1")))

print("ΕΥΑΙΣΘΗΤΟΙ ΠΙΝΑΚΕΣ (πρέπει μπλοκ):")
for tbl in ["businesses", "subscriptions", "customer_memory", "voice_calls", "voice_agents", "audit_logs"]:
    chk(f"{tbl} μπλοκ", "error" in qd("clinicA", f"SELECT * FROM {tbl} WHERE slug='clinicA' OR 1=1"))
chk("comma-join σε ευαίσθητο μπλοκ", "error" in qd("clinicA", "SELECT * FROM bookings, businesses WHERE 1=1"))
chk("mutation (DELETE) μπλοκ", "error" in qd("clinicA", "DELETE FROM bookings WHERE slug='clinicA'"))
chk("multi-statement μπλοκ", "error" in qd("clinicA", "SELECT 1; DROP TABLE bookings"))

print("=" * 50)
if fails:
    print(f"❌ {len(fails)} FAILURES: {fails}"); sys.exit(1)
print("✅ query_data ΑΣΦΑΛΕΣ: μηδέν cross-tenant leak, secrets protected, valid queries OK.")
