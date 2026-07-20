"""
routes/testing_routes.py — Οπτική Σελίδα Ελέγχου (Testing Dashboard) για τον ιδιοκτήτη.
================================================================================
Σκοπός: ο Ντίνος (χωρίς γνώσεις κώδικα) να ΒΛΕΠΕΙ live αν το σύστημα δουλεύει στην πράξη —
τι μπαίνει στη βάση, αν οι λειτουργίες εκτελούνται όντως, και ΤΙ έσπασε (σε απλά ελληνικά).

Η βάση είναι SQLite (ΟΧΙ MySQL) — ένα αρχείο. Αυτή η σελίδα το οπτικοποιεί χωρίς εξωτερικά εργαλεία.
ΟΛΑ τα test δεδομένα ζουν σε ΑΠΟΜΟΝΩΜΕΝΟ sandbox ιατρείο (slug='testing-sandbox') → «Καθαρισμός»
δεν αγγίζει ΠΟΤΕ πραγματικά δεδομένα πελατών. Admin-gated (ADMIN_TOKEN).

  GET  /admin/testing                     → η σελίδα (HTML)
  GET  /api/admin/testing/data            → live snapshot της βάσης (JSON)
  POST /api/admin/testing/fake-patient    → φτιάχνει ψεύτικο ασθενή
  POST /api/admin/testing/simulate-booking→ τρέχει ΟΛΗ τη ροή (κράτηση→επίσκεψη→τιμολόγιο) & δείχνει κάθε βήμα
  POST /api/admin/testing/cleanup         → σβήνει ΜΟΝΟ τα test δεδομένα (reset sandbox)
"""
import os
import random
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify

import database as db

bp = Blueprint("testing", __name__)
SANDBOX = "testing-sandbox"

_NAMES = ["Γιώργος Παπαδόπουλος", "Μαρία Νικολάου", "Ελένη Γεωργίου", "Δημήτρης Ιωάννου",
          "Κατερίνα Βασιλείου", "Νίκος Αντωνίου", "Σοφία Δημητρίου", "Κώστας Παππάς",
          "Αναστασία Μακρή", "Θανάσης Οικονόμου"]
_SERVICES = ["Φυσικοθεραπεία", "Αξιολόγηση", "Συνεδρία", "Επανεξέταση"]

# μετάφραση action codes της audit αλυσίδας σε απλά ελληνικά (για το «μαύρο κουτί»)
_ACTION_GR = {
    "bridge.booking.requested": "Ήρθε αίτημα ραντεβού",
    "bridge.appointment.booked": "✅ Έκλεισε ραντεβού",
    "bridge.appointment.rejected": "⚠️ Απορρίφθηκε (η ώρα ήταν πιασμένη)",
    "bridge.checkin.prepared": "✅ Ετοιμάστηκε ο φάκελος του ασθενή",
    "bridge.patient.arrived": "Ο ασθενής έφτασε",
    "bridge.visit.started": "Ξεκίνησε η επίσκεψη",
    "bridge.visit.completed": "Ολοκληρώθηκε η επίσκεψη",
    "bridge.note.signed": "✅ Γράφτηκε ιατρική σημείωση στον φάκελο",
    "bridge.visit.billable": "Η επίσκεψη έγινε χρεώσιμη",
    "bridge.invoice.issued": "✅ Εκδόθηκε τιμολόγιο",
    "bridge.checkout.done": "✅ Κλείστηκε follow-up + υπενθύμιση",
    "autoloop.cycle": "🔄 Ο αυτόνομος βρόχος έτρεξε (έμαθε από νέα δεδομένα)",
    "autoloop.recovery_enqueued": "📲 Προγραμματίστηκε μήνυμα ανάκτησης",
    "chat_completed": "💬 Απάντησε ο AI βοηθός",
}


def _gate():
    from admin_auth import admin_ok, token_from_request
    return admin_ok(token_from_request())


def _friendly_error(e: Exception) -> str:
    """Μεταφράζει τεχνικά σφάλματα σε απλά ελληνικά ώστε ο Ντίνος να καταλάβει ΤΙ έσπασε."""
    s = str(e).lower()
    if "unable to open database" in s or "no such file" in s:
        return "Η βάση δεδομένων δεν βρέθηκε ή δεν ανοίγει (πρόβλημα με το αρχείο/δίσκο)."
    if "no such table" in s or "no such column" in s:
        return f"Λείπει ένας πίνακας ή στήλη από τη βάση (λάθος schema): {e}"
    if "database is locked" in s:
        return "Η βάση είναι προσωρινά κλειδωμένη (πολλές ταυτόχρονες εγγραφές) — δοκίμασε ξανά."
    if "double_booking" in s:
        return "Η ώρα που δοκίμασε ήταν ήδη πιασμένη (ο έλεγχος διπλοκράτησης δούλεψε σωστά)."
    if "past_date" in s:
        return "Η ημερομηνία ήταν στο παρελθόν (το σύστημα σωστά την απέρριψε)."
    if "unauthorized" in s or "403" in s or "401" in s:
        return "Δεν έχεις δικαίωμα πρόσβασης (λάθος admin token)."
    return f"Απρόσμενο σφάλμα: {e}"


def _ensure_sandbox():
    import provisioning
    provisioning.provision("🧪 ΔΟΚΙΜΑΣΤΙΚΟ ΙΑΤΡΕΙΟ (sandbox)", "physio",
                           owner_name="Testing", email="testing@ran-tevu.gr",
                           demo=False, slug=SANDBOX)


# ─────────────────────────────── DATA (live snapshot) ───────────────────────
@bp.get("/api/admin/testing/data")
def testing_data():
    if not _gate():
        return jsonify({"error": "unauthorized"}), 401
    out = {"db": {}, "counts": {}, "patients": [], "bookings": [], "invoices": [], "activity": []}
    # 1) DB connection status
    try:
        from database import DB as _DB
        path = str(_DB)
        exists = os.path.exists(path) if path != ":memory:" else True
        with db._conn() as c:
            c.execute("SELECT 1")
        out["db"] = {"type": "SQLite", "path": path, "exists": exists, "ok": True, "error": None}
    except Exception as e:
        out["db"] = {"type": "SQLite", "ok": False, "error": _friendly_error(e)}
        return jsonify(out)   # χωρίς σύνδεση, δεν έχει νόημα να συνεχίσουμε
    try:
        _ensure_sandbox()
        with db._conn() as c:
            out["counts"] = {
                "patients": c.execute("SELECT COUNT(*) FROM bo_patients WHERE slug=?", (SANDBOX,)).fetchone()[0],
                "bookings": c.execute("SELECT COUNT(*) FROM bookings WHERE slug=?", (SANDBOX,)).fetchone()[0],
                "invoices": c.execute("SELECT COUNT(*) FROM bo_invoices WHERE slug=?", (SANDBOX,)).fetchone()[0],
            }
            out["patients"] = [dict(r) for r in c.execute(
                "SELECT name, phone, email, amka, created FROM bo_patients WHERE slug=? ORDER BY id DESC LIMIT 15", (SANDBOX,))]
            out["bookings"] = [dict(r) for r in c.execute(
                "SELECT id, date, time, service, name, phone, status FROM bookings WHERE slug=? ORDER BY id DESC LIMIT 15", (SANDBOX,))]
            out["invoices"] = [dict(r) for r in c.execute(
                "SELECT id, patient_name, total, status, mydata_status FROM bo_invoices WHERE slug=? ORDER BY id DESC LIMIT 15", (SANDBOX,))]
            acts = c.execute(
                "SELECT action, actor_id, details, created FROM audit_logs WHERE slug=? ORDER BY id DESC LIMIT 25", (SANDBOX,)).fetchall()
            out["activity"] = [{"when": a["created"], "what": _ACTION_GR.get(a["action"], a["action"]),
                                "who": a["actor_id"] or "", "details": a["details"] or ""} for a in acts]
    except Exception as e:
        out["error"] = _friendly_error(e)
    return jsonify(out)


# ─────────────────────────────── ΚΟΥΜΠΙ 1: Ψεύτικος Ασθενής ─────────────────
@bp.post("/api/admin/testing/fake-patient")
def testing_fake_patient():
    if not _gate():
        return jsonify({"ok": False, "error": "Δεν έχεις δικαίωμα (λάθος admin token)."}), 401
    try:
        _ensure_sandbox()
        import back_office as bo
        name = random.choice(_NAMES)
        phone = "69" + "".join(str(random.randint(0, 9)) for _ in range(8))
        amka = "".join(str(random.randint(0, 9)) for _ in range(11))
        email = f"test{random.randint(1000,9999)}@example.gr"
        bo.upsert_patient(SANDBOX, phone, name, email, amka=amka)
        return jsonify({"ok": True, "steps": [
            f"✅ Δημιουργήθηκε ασθενής: {name}",
            f"✅ Αποθηκεύτηκε στη βάση (τηλ: {phone}, ΑΜΚΑ: {amka})",
            "✅ Δες τον πίνακα «Ασθενείς» παρακάτω — πρέπει να εμφανίστηκε τώρα.",
        ]})
    except Exception as e:
        return jsonify({"ok": False, "error": _friendly_error(e)})


# ─────────────────────────── ΚΟΥΜΠΙ 2: Προσομοίωση Ραντεβού (πλήρης ροή) ────
@bp.post("/api/admin/testing/simulate-booking")
def testing_simulate_booking():
    if not _gate():
        return jsonify({"ok": False, "error": "Δεν έχεις δικαίωμα (λάθος admin token)."}), 401
    try:
        _ensure_sandbox()
        from clinic_bridge import ClinicBridge
        name = random.choice(_NAMES)
        phone = "69" + "".join(str(random.randint(0, 9)) for _ in range(8))
        # τυχαία μελλοντική ώρα → αποφυγή σύγκρουσης σε επαναλαμβανόμενα κλικ
        d = (datetime.now() + timedelta(days=random.randint(1, 60))).strftime("%Y-%m-%d")
        t = f"{random.randint(9,17):02d}:{random.choice(['00','30'])}"
        db.log_consent(consent_type="reminders", slug=SANDBOX, identifier=phone, accepted=True)  # ώστε να τρέξει η υπενθύμιση
        br = ClinicBridge(SANDBOX)
        # Η ΠΛΗΡΗΣ ροή: Γραμματέας κλείνει → ασθενής φτάνει → Γιατρός ολοκληρώνει
        br.request_booking(name, phone, t, d, service=random.choice(_SERVICES), value=40.0)
        booked = next((e for e in br.journey() if e["type"] == "appointment.booked"), None)
        if not booked:
            return jsonify({"ok": False, "error": "Η κράτηση δεν ολοκληρώθηκε (η ώρα ήταν πιασμένη — δοκίμασε ξανά)."})
        bid = booked["payload"]["booking_id"]
        br.arrive(bid, name, phone)
        br.complete_visit(bid, name, phone, notes="Ο ασθενής ανέφερε οσφυαλγία. Πρόταση: φυσικοθεραπεία.", value=40.0)
        # μετάφραση της ροής σε βήματα που καταλαβαίνει ο Ντίνος
        steps, ok_flags = [], {"appointment.booked": "1️⃣ Η Γραμματέας έκλεισε το ραντεβού",
                               "checkin.prepared": "2️⃣ Ετοιμάστηκε ο φάκελος του ασθενή",
                               "visit.started": "3️⃣ Ξεκίνησε η επίσκεψη",
                               "note.signed": "4️⃣ Ο Γιατρός έγραψε ιατρική σημείωση",
                               "invoice.issued": "5️⃣ Εκδόθηκε τιμολόγιο αυτόματα",
                               "checkout.done": "6️⃣ Κλείστηκε follow-up + υπενθύμιση"}
        seen = {e["type"] for e in br.journey()}
        for k, label in ok_flags.items():
            steps.append((("✅ " + label) if k in seen else ("❌ ΔΕΝ έγινε: " + label)))
        with db._conn() as c:
            inv = c.execute("SELECT COUNT(*) FROM bo_invoices WHERE slug=? AND booking_id=?", (SANDBOX, bid)).fetchone()[0]
        steps.append(f"➡️ Αποτέλεσμα: ραντεβού #{bid} για {name} στις {d} {t} · τιμολόγια: {inv}")
        return jsonify({"ok": True, "steps": steps})
    except Exception as e:
        return jsonify({"ok": False, "error": _friendly_error(e)})


# ─────────────────────────────── ΚΟΥΜΠΙ 3: Καθαρισμός ──────────────────────
@bp.post("/api/admin/testing/cleanup")
def testing_cleanup():
    if not _gate():
        return jsonify({"ok": False, "error": "Δεν έχεις δικαίωμα (λάθος admin token)."}), 401
    try:
        import provisioning
        _ensure_sandbox()
        provisioning.reset_business_data(SANDBOX)   # σβήνει ΜΟΝΟ το sandbox
        with db._conn() as c:
            for tbl in ("audit_logs", "consent_logs", "outbound_jobs", "autoloop_state"):
                try:
                    c.execute(f"DELETE FROM {tbl} WHERE slug=?", (SANDBOX,))
                except Exception:
                    pass
            c.commit()
        return jsonify({"ok": True, "steps": [
            "✅ Καθαρίστηκαν ΟΛΑ τα δοκιμαστικά δεδομένα (ασθενείς, ραντεβού, τιμολόγια, logs).",
            "✅ Τα ΠΡΑΓΜΑΤΙΚΑ δεδομένα πελατών ΔΕΝ πειράχτηκαν (διαφορετικό ιατρείο).",
        ]})
    except Exception as e:
        return jsonify({"ok": False, "error": _friendly_error(e)})


# ─────────────────────── SQL ΚΟΝΣΟΛΑ (read-only) & SCHEMA ───────────────────
_SQL_BLOCK = ("insert", "update", "delete", "drop", "alter", "create", "replace",
              "attach", "detach", "vacuum", "pragma", "reindex", "truncate", "grant")


@bp.get("/api/admin/testing/schema")
def testing_schema():
    """Λίστα ΟΛΩΝ των πινάκων + πλήθος γραμμών + στήλες — «δες τι υπάρχει στη βάση»."""
    if not _gate():
        return jsonify({"error": "unauthorized"}), 401
    try:
        with db._conn() as c:
            tabs = [r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            out = []
            for t in tabs:
                try:
                    n = c.execute(f"SELECT COUNT(*) FROM \"{t}\"").fetchone()[0]
                    cols = [r["name"] for r in c.execute(f"PRAGMA table_info(\"{t}\")")]
                    out.append({"table": t, "rows": n, "columns": cols})
                except Exception:
                    pass
        return jsonify({"tables": out})
    except Exception as e:
        return jsonify({"error": _friendly_error(e)})


@bp.post("/api/admin/testing/sql")
def testing_sql():
    """Εκτέλεση ΜΟΝΟ read-only SQL (SELECT/WITH) — «μίλα με τα δεδομένα σου». Γράψιμο μπλοκάρεται
    εδώ για ασφάλεια (μια λάθος DELETE θα έσβηνε δεδομένα). Για διορθώσεις: πες μου τι να αλλάξω."""
    if not _gate():
        return jsonify({"ok": False, "error": "Δεν έχεις δικαίωμα (λάθος admin token)."}), 401
    q = (request.get_json(silent=True) or {}).get("query", "").strip().rstrip(";").strip()
    if not q:
        return jsonify({"ok": False, "error": "Γράψε ένα ερώτημα SQL (π.χ. SELECT * FROM bo_patients)."})
    low = q.lower()
    if ";" in q:
        return jsonify({"ok": False, "error": "Ένα ερώτημα τη φορά (αφαίρεσε το ';' και ό,τι έχει μετά)."})
    if not (low.startswith("select") or low.startswith("with")):
        return jsonify({"ok": False, "error": "Επιτρέπονται ΜΟΝΟ ερωτήματα ανάγνωσης (SELECT). "
                                              "Για αλλαγές/διορθώσεις δεδομένων, πες μου τι θέλεις να αλλάξω."})
    if any(f" {w} " in f" {low} " or low.startswith(w + " ") for w in _SQL_BLOCK):
        return jsonify({"ok": False, "error": "Το ερώτημα περιέχει εντολή που αλλάζει δεδομένα — μπλοκαρίστηκε για ασφάλεια."})
    try:
        with db._conn() as c:
            rows = c.execute(q).fetchall()
        cols = list(rows[0].keys()) if rows else []
        data = [list(r) for r in rows[:500]]      # cap 500 γραμμές
        return jsonify({"ok": True, "columns": cols, "rows": data,
                        "rowcount": len(rows), "capped": len(rows) > 500})
    except Exception as e:
        return jsonify({"ok": False, "error": _friendly_error(e)})


@bp.get("/api/admin/testing/download-db")
def testing_download_db():
    """Κατέβασε το αρχείο της βάσης (SQLite) για άνοιγμα σε DBeaver/TablePlus. ⚠️ Περιέχει
    πραγματικά δεδομένα ασθενών — κράτησέ το ασφαλές, admin-gated."""
    if not _gate():
        return jsonify({"error": "unauthorized"}), 401
    try:
        from flask import send_file
        from database import DB as _DB
        path = str(_DB)
        if path == ":memory:" or not os.path.exists(path):
            return jsonify({"error": "Η βάση είναι in-memory ή δεν βρέθηκε αρχείο."}), 404
        return send_file(path, as_attachment=True, download_name="ran-tevu-database.db")
    except Exception as e:
        return jsonify({"error": _friendly_error(e)}), 500


# ─────────────────────────────────── HTML ──────────────────────────────────
@bp.get("/admin/testing")
def testing_page():
    return _PAGE


def register(app):
    app.register_blueprint(bp)
    print("[TESTING] οπτική σελίδα ελέγχου loaded — /admin/testing")


_PAGE = r"""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="robots" content="noindex,nofollow"/>
<title>🧪 Σελίδα Ελέγχου — Ran-Tevu</title>
<style>
 *{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
 body{background:#0f1419;color:#e6e9ee;padding:18px 14px;max-width:1000px;margin:0 auto}
 h1{font-size:22px;margin-bottom:4px}.sub{color:#8b95a5;font-size:13px;margin-bottom:16px}
 .card{background:#1a2029;border:1px solid #2a323d;border-radius:14px;padding:16px 18px;margin-bottom:14px}
 .db-ok{color:#22c55e;font-weight:700}.db-bad{color:#ef4444;font-weight:700}
 .tokrow{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
 .tokrow input{flex:1;min-width:180px;padding:10px 12px;background:#0f1419;border:1px solid #2a323d;border-radius:9px;color:#e6e9ee;font-size:14px}
 .btns{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:6px}
 .btn{padding:18px;border:0;border-radius:12px;font-size:16px;font-weight:700;cursor:pointer;color:#fff}
 .b1{background:#2563eb}.b2{background:#16a34a}.b3{background:#dc2626}.btn:active{transform:scale(.98)}.btn:disabled{opacity:.5}
 .result{margin-top:12px;padding:14px 16px;border-radius:11px;font-size:14px;line-height:1.9;display:none}
 .result.ok{display:block;background:#0d2818;border:1px solid #16a34a}
 .result.err{display:block;background:#2b0f10;border:2px solid #ef4444}
 .result.err .t{color:#ff6b6b;font-weight:800;font-size:16px;margin-bottom:6px}
 table{width:100%;border-collapse:collapse;font-size:12.5px}
 th{text-align:left;color:#8b95a5;font-weight:600;padding:7px 8px;border-bottom:1px solid #2a323d;font-size:11px;text-transform:uppercase}
 td{padding:7px 8px;border-bottom:1px solid #222933}
 .st{padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600}
 .st.confirmed,.st.unpaid{background:#1e3a5f;color:#7db3f0}.st.completed,.st.paid{background:#0d2818;color:#4ade80}
 .st.no_show,.st.cancelled{background:#3a1a1a;color:#f87171}
 .empty{color:#5a6472;text-align:center;padding:18px;font-size:13px}
 .cnt{display:inline-block;background:#252d38;border-radius:8px;padding:4px 12px;margin-right:8px;font-size:13px}
 .cnt b{color:#60a5fa;font-size:16px}
 h3{font-size:14px;margin-bottom:10px;color:#c5ccd6}
 .mono{font-family:ui-monospace,monospace;color:#8b95a5}
 .act{font-size:12.5px;padding:5px 0;border-bottom:1px solid #222933;display:flex;gap:10px}
 .act .w{color:#5a6472;min-width:64px;font-family:ui-monospace,monospace}
</style></head><body>
<h1>🧪 Σελίδα Ελέγχου (Testing)</h1>
<div class="sub">Δες live αν το σύστημα δουλεύει στην πράξη. Τα δοκιμαστικά δεδομένα ζουν σε απομονωμένο «δοκιμαστικό ιατρείο» — τα πραγματικά ΔΕΝ πειράζονται.</div>

<div class="card">
 <div class="tokrow">
  <input id="tok" type="password" placeholder="Βάλε το admin token σου εδώ"/>
  <button class="btn b1" style="padding:10px 16px;font-size:14px" onclick="saveTok()">Σύνδεση</button>
 </div>
 <div id="dbstat">Βάση: <span class="mono">— (βάλε token & πάτα Σύνδεση)</span></div>
</div>

<div class="card">
 <h3>Κουμπιά Δοκιμών</h3>
 <div class="btns">
  <button class="btn b1" onclick="run('fake-patient',this)">👤 Δημιουργία Ψεύτικου Ασθενή</button>
  <button class="btn b2" onclick="run('simulate-booking',this)">📅 Προσομοίωση Ραντεβού<br><small style="font-weight:400;opacity:.85">(όλη η ροή: κράτηση→επίσκεψη→τιμολόγιο)</small></button>
  <button class="btn b3" onclick="run('cleanup',this)">🧹 Καθαρισμός Βάσης</button>
 </div>
 <div id="result" class="result"></div>
</div>

<div class="card">
 <div id="counts"></div>
</div>

<div class="card">
 <h3>💬 Μίλα με τα δεδομένα σου (SQL κονσόλα)</h3>
 <div class="sub" style="margin-bottom:10px">Γράψε ένα ερώτημα ή πάτα ένα από τα έτοιμα. Μόνο ανάγνωση (SELECT) — ασφαλές, δεν αλλάζει τίποτα.</div>
 <div id="quick" style="display:flex;gap:7px;flex-wrap:wrap;margin-bottom:10px"></div>
 <textarea id="sql" rows="3" style="width:100%;padding:11px;background:#0f1419;border:1px solid #2a323d;border-radius:9px;color:#e6e9ee;font-family:ui-monospace,monospace;font-size:13px" placeholder="SELECT * FROM bo_patients LIMIT 20"></textarea>
 <div style="margin-top:8px"><button class="btn b1" style="padding:11px 18px;font-size:14px" onclick="runSql()">▶ Εκτέλεση</button></div>
 <div id="sqlresult" style="margin-top:12px"></div>
</div>

<div class="card">
 <h3>🗂️ Πίνακες στη βάση σου (κλικ για να δεις περιεχόμενα)</h3>
 <div id="schema"><div class="empty">—</div></div>
 <div style="margin-top:12px;border-top:1px solid #2a323d;padding-top:12px">
  <a id="dl" href="#" onclick="dlDb();return false" style="color:#60a5fa;text-decoration:none;font-size:13px">⬇️ Κατέβασε τη βάση (για άνοιγμα σε DBeaver/TablePlus)</a>
  <div class="sub" style="margin-top:4px">⚠️ Περιέχει πραγματικά δεδομένα ασθενών — κράτησέ το ασφαλές.</div>
 </div>
</div>

<div class="card"><h3>🩺 Ασθενείς — δοκιμαστικό ιατρείο (live)</h3><div id="tbl-patients"><div class="empty">—</div></div></div>
<div class="card"><h3>📅 Ραντεβού (live)</h3><div id="tbl-bookings"><div class="empty">—</div></div></div>
<div class="card"><h3>🧾 Τιμολόγια (live)</h3><div id="tbl-invoices"><div class="empty">—</div></div></div>
<div class="card"><h3>📻 Ιστορικό ενεργειών — «μαύρο κουτί» (τι έκανε το σύστημα)</h3><div id="tbl-activity"><div class="empty">—</div></div></div>

<script>
let TOK=localStorage.getItem('rt_test_tok')||'';
document.getElementById('tok').value=TOK;
function saveTok(){TOK=document.getElementById('tok').value.trim();localStorage.setItem('rt_test_tok',TOK);load();}
function esc(s){return (s==null?'':''+s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function showResult(ok,steps,err){const r=document.getElementById('result');
 if(err){r.className='result err';r.innerHTML='<div class="t">🔴 Κάτι πήγε στραβά</div>'+esc(err)+'<div style="margin-top:8px;font-size:12.5px;color:#c99">Αντίγραψέ το και δώσ\' το στον Claude να το φτιάξει.</div>';}
 else{r.className='result ok';r.innerHTML=(steps||[]).map(esc).join('<br>');}}
async function run(action,btn){if(!TOK){showResult(false,null,'Βάλε πρώτα το admin token και πάτα Σύνδεση.');return;}
 btn.disabled=true;const orig=btn.innerHTML;btn.innerHTML='⏳ Τρέχει…';
 try{const r=await fetch('/api/admin/testing/'+action,{method:'POST',headers:{'X-Admin-Token':TOK,'Authorization':'Bearer '+TOK}});
  const j=await r.json();showResult(j.ok,j.steps,j.error);}
 catch(e){showResult(false,null,'Δεν απάντησε ο server: '+e.message);}
 btn.disabled=false;btn.innerHTML=orig;load();}
function tbl(rows,cols){if(!rows||!rows.length)return '<div class="empty">Κανένα δεδομένο ακόμη — πάτα ένα κουμπί πάνω.</div>';
 return '<div style="overflow-x:auto"><table><thead><tr>'+cols.map(c=>'<th>'+c[1]+'</th>').join('')+'</tr></thead><tbody>'+
  rows.map(r=>'<tr>'+cols.map(c=>{let v=r[c[0]];if(c[0]==='status')return '<td><span class="st '+esc(v)+'">'+esc(v)+'</span></td>';return '<td>'+esc(v)+'</td>';}).join('')+'</tr>').join('')+'</tbody></table></div>';}
async function load(){if(!TOK)return;
 try{const r=await fetch('/api/admin/testing/data',{headers:{'X-Admin-Token':TOK,'Authorization':'Bearer '+TOK}});
  if(r.status===401){document.getElementById('dbstat').innerHTML='<span class="db-bad">🔴 Λάθος admin token</span>';return;}
  const j=await r.json();
  const db=j.db||{};
  document.getElementById('dbstat').innerHTML = db.ok
    ? 'Βάση: <span class="db-ok">🟢 SQLite συνδεδεμένη</span> <span class="mono">('+esc(db.path)+')</span>'
    : 'Βάση: <span class="db-bad">🔴 ΠΡΟΒΛΗΜΑ: '+esc(db.error)+'</span>';
  const c=j.counts||{};
  document.getElementById('counts').innerHTML='<span class="cnt">Ασθενείς <b>'+(c.patients||0)+'</b></span><span class="cnt">Ραντεβού <b>'+(c.bookings||0)+'</b></span><span class="cnt">Τιμολόγια <b>'+(c.invoices||0)+'</b></span>';
  document.getElementById('tbl-patients').innerHTML=tbl(j.patients,[['name','Όνομα'],['phone','Τηλέφωνο'],['email','Email'],['amka','ΑΜΚΑ'],['created','Δημιουργήθηκε']]);
  document.getElementById('tbl-bookings').innerHTML=tbl(j.bookings,[['id','#'],['date','Ημ/νία'],['time','Ώρα'],['service','Υπηρεσία'],['name','Ασθενής'],['status','Κατάσταση']]);
  document.getElementById('tbl-invoices').innerHTML=tbl(j.invoices,[['id','#'],['patient_name','Ασθενής'],['total','Ποσό €'],['status','Κατάσταση'],['mydata_status','myDATA']]);
  const acts=j.activity||[];
  document.getElementById('tbl-activity').innerHTML=acts.length?acts.map(a=>'<div class="act"><span class="w">'+esc((a.when||'').slice(11,19))+'</span><span>'+esc(a.what)+(a.details?' <span class="mono">('+esc(a.details)+')</span>':'')+'</span></div>').join(''):'<div class="empty">Καμία ενέργεια ακόμη.</div>';
 }catch(e){document.getElementById('dbstat').innerHTML='<span class="db-bad">🔴 Ο server δεν απαντά: '+esc(e.message)+'</span>';}}
// ── SQL κονσόλα ──
const QUICK=[
 ['Όλοι οι ασθενείς','SELECT name, phone, email, amka FROM bo_patients ORDER BY id DESC LIMIT 30'],
 ['Όλα τα ραντεβού','SELECT id, slug, date, time, service, name, status FROM bookings ORDER BY id DESC LIMIT 30'],
 ['Σημερινά ραντεβού',"SELECT slug, time, service, name, status FROM bookings WHERE date=date('now') ORDER BY time"],
 ['Τιμολόγια','SELECT id, slug, patient_name, total, status, mydata_status FROM bo_invoices ORDER BY id DESC LIMIT 30'],
 ['Τελευταία logs','SELECT created, action, actor_id, details FROM audit_logs ORDER BY id DESC LIMIT 40'],
 ['Επιχειρήσεις','SELECT slug, name, type FROM businesses'],
];
function qk(i){document.getElementById('sql').value=QUICK[i][1];runSql();}
function qt(name){document.getElementById('sql').value='SELECT * FROM '+name+' LIMIT 30';runSql();window.scrollTo(0,0);}
function renderQuick(){document.getElementById('quick').innerHTML=QUICK.map((q,i)=>'<button onclick="qk('+i+')" style="background:#252d38;color:#c5ccd6;border:1px solid #2a323d;border-radius:8px;padding:7px 11px;font-size:12px;cursor:pointer">'+esc(q[0])+'</button>').join('');}
async function runSql(){if(!TOK){showResult(false,null,'Βάλε πρώτα το admin token.');return;}
 const q=document.getElementById('sql').value;const box=document.getElementById('sqlresult');box.innerHTML='<div class="empty">Τρέχει…</div>';
 try{const r=await fetch('/api/admin/testing/sql',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+TOK},body:JSON.stringify({query:q})});
  const j=await r.json();
  if(!j.ok){box.innerHTML='<div class="result err" style="display:block"><div class="t">🔴 Το ερώτημα δεν έτρεξε</div>'+esc(j.error)+'</div>';return;}
  if(!j.columns.length){box.innerHTML='<div class="empty">Κανένα αποτέλεσμα (0 γραμμές).</div>';return;}
  let h='<div style="color:#8b95a5;font-size:12px;margin-bottom:6px">'+j.rowcount+' γραμμές'+(j.capped?' (δείχνω τις πρώτες 500)':'')+'</div><div style="overflow-x:auto"><table><thead><tr>'+j.columns.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr></thead><tbody>'+
   j.rows.map(row=>'<tr>'+row.map(v=>'<td>'+esc(v)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
  box.innerHTML=h;}
 catch(e){box.innerHTML='<div class="result err" style="display:block">Ο server δεν απάντησε: '+esc(e.message)+'</div>';}}
async function loadSchema(){if(!TOK)return;
 try{const r=await fetch('/api/admin/testing/schema',{headers:{'Authorization':'Bearer '+TOK}});const j=await r.json();
  if(!j.tables){document.getElementById('schema').innerHTML='<div class="empty">—</div>';return;}
  document.getElementById('schema').innerHTML=j.tables.map(t=>'<span onclick="qt(\''+t.table+'\')" style="display:inline-block;background:#252d38;border:1px solid #2a323d;border-radius:8px;padding:6px 11px;margin:0 6px 6px 0;font-size:12px;cursor:pointer"><b style="color:#60a5fa">'+esc(t.table)+'</b> <span style="color:#5a6472">('+t.rows+')</span></span>').join('');}
 catch(e){}}
function dlDb(){if(!TOK){alert('Βάλε πρώτα το admin token.');return;}
 window.location='/api/admin/testing/download-db?token='+encodeURIComponent(TOK);}
renderQuick();
if(TOK){load();loadSchema();}
setInterval(()=>{if(TOK){load();loadSchema();}},5000);
</script></body></html>"""
