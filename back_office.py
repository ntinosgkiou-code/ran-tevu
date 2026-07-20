"""
back_office.py — Deep Back-Office Data Layer (το "laptop" του ιατρείου)

Μοντελοποιεί κάθε λειτουργία που σήμερα γίνεται χειροκίνητα στο laptop της
γραμματείας ενός ιατρείου/φυσικοθεραπευτηρίου/beauty clinic ως δομημένα δεδομένα
+ autonomous agents που τα εκτελούν. Όλα τα entities συνδέονται μεταξύ τους
"σαν Obsidian" μέσω ενός knowledge-graph (bo_links): ασθενής ↔ ραντεβού ↔
τιμολόγιο ↔ επικοινωνία ↔ έγγραφο ↔ ανάλωσιμο. Multi-tenant (slug-based),
stateless, ground-truth στη SQLite.

Κοινά σημεία cross-vertical (physio / γιατροί / δερματολόγος / beauty):
  1. CRM / Ψηφιακός Φάκελος Ασθενή   → bo_patients, bo_patient_notes, bo_documents
  2. Calendar / Chair-time           → util πάνω στο υπάρχον bookings table
  3. Financial / myDATA / Υπόλοιπα    → bo_invoices, bo_payments, bo_expenses
  4. Communication Hub / Recall      → bo_comm_log
  5. Suppliers / Inventory           → bo_suppliers, bo_inventory, bo_supplier_orders
  6. Obsidian Graph                  → bo_links (bidirectional rel)
"""
import json
import sqlite3
import threading
from datetime import datetime, timedelta

from database import (_conn, get_business, recovered_revenue, recovered_events,
                      mark_booking_outcome, get_booking)
from crypto_util import encrypt, decrypt


# ════════════════════════════════════════════════════════════════════════════
# SCHEMA
# ════════════════════════════════════════════════════════════════════════════
def init_back_office():
    """Δημιουργεί όλα τα back-office tables (idempotent). Κάλεσέ το στο startup."""
    with _conn() as c:
        # 1) Ψηφιακός Φάκελος Ασθενή ----------------------------------------
        c.execute("""
            CREATE TABLE IF NOT EXISTS bo_patients (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                slug            TEXT NOT NULL,
                phone           TEXT NOT NULL,
                name            TEXT DEFAULT '',
                email           TEXT DEFAULT '',
                dob             TEXT DEFAULT '',
                gender          TEXT DEFAULT '',
                amka            TEXT DEFAULT '',
                address         TEXT DEFAULT '',
                occupation      TEXT DEFAULT '',
                referral_source TEXT DEFAULT '',
                medical_alerts  TEXT DEFAULT '',
                tags            TEXT DEFAULT '[]',
                status          TEXT DEFAULT 'active',
                created         TEXT DEFAULT (datetime('now')),
                updated         TEXT DEFAULT (datetime('now')),
                UNIQUE(slug, phone)
            )""")

        # Φάκελος entries: εξετάσεις, διαγνώσεις, πλάνο θεραπείας, σημειώσεις
        c.execute("""
            CREATE TABLE IF NOT EXISTS bo_patient_notes (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                slug          TEXT NOT NULL,
                patient_phone TEXT NOT NULL,
                kind          TEXT DEFAULT 'clinical',
                title         TEXT DEFAULT '',
                body          TEXT DEFAULT '',
                author        TEXT DEFAULT 'system',
                booking_id    INTEGER,
                created       TEXT DEFAULT (datetime('now'))
            )""")

        c.execute("""
            CREATE TABLE IF NOT EXISTS bo_documents (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                slug          TEXT NOT NULL,
                patient_phone TEXT NOT NULL,
                title         TEXT DEFAULT '',
                doc_type      TEXT DEFAULT 'other',
                ref           TEXT DEFAULT '',
                created       TEXT DEFAULT (datetime('now'))
            )""")

        # 3) Financial / myDATA --------------------------------------------
        c.execute("""
            CREATE TABLE IF NOT EXISTS bo_invoices (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                slug          TEXT NOT NULL,
                number        TEXT DEFAULT '',
                patient_phone TEXT DEFAULT '',
                patient_name  TEXT DEFAULT '',
                issue_date    TEXT DEFAULT (date('now')),
                due_date      TEXT DEFAULT '',
                service       TEXT DEFAULT '',
                amount        REAL DEFAULT 0,
                vat_rate      REAL DEFAULT 0,
                vat           REAL DEFAULT 0,
                total         REAL DEFAULT 0,
                paid          REAL DEFAULT 0,
                status        TEXT DEFAULT 'unpaid',
                mydata_status TEXT DEFAULT 'pending',
                mydata_mark   TEXT DEFAULT '',
                booking_id    INTEGER,
                created       TEXT DEFAULT (datetime('now'))
            )""")

        c.execute("""
            CREATE TABLE IF NOT EXISTS bo_payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                slug        TEXT NOT NULL,
                invoice_id  INTEGER NOT NULL,
                amount      REAL DEFAULT 0,
                method      TEXT DEFAULT 'cash',
                paid_at     TEXT DEFAULT (datetime('now'))
            )""")

        c.execute("""
            CREATE TABLE IF NOT EXISTS bo_expenses (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                slug         TEXT NOT NULL,
                category     TEXT DEFAULT 'γενικά',
                vendor       TEXT DEFAULT '',
                amount       REAL DEFAULT 0,
                vat          REAL DEFAULT 0,
                expense_date TEXT DEFAULT (date('now')),
                note         TEXT DEFAULT '',
                created      TEXT DEFAULT (datetime('now'))
            )""")

        # 5) Suppliers / Inventory -----------------------------------------
        c.execute("""
            CREATE TABLE IF NOT EXISTS bo_suppliers (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                slug      TEXT NOT NULL,
                name      TEXT DEFAULT '',
                category  TEXT DEFAULT '',
                contact   TEXT DEFAULT '',
                phone     TEXT DEFAULT '',
                email     TEXT DEFAULT '',
                notes     TEXT DEFAULT '',
                created   TEXT DEFAULT (datetime('now'))
            )""")

        c.execute("""
            CREATE TABLE IF NOT EXISTS bo_inventory (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                slug          TEXT NOT NULL,
                sku           TEXT DEFAULT '',
                name          TEXT DEFAULT '',
                category      TEXT DEFAULT '',
                qty           REAL DEFAULT 0,
                unit          TEXT DEFAULT 'τεμ',
                reorder_level REAL DEFAULT 0,
                unit_cost     REAL DEFAULT 0,
                supplier_id   INTEGER,
                updated       TEXT DEFAULT (datetime('now'))
            )""")

        c.execute("""
            CREATE TABLE IF NOT EXISTS bo_supplier_orders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                slug        TEXT NOT NULL,
                supplier_id INTEGER,
                items       TEXT DEFAULT '[]',
                total       REAL DEFAULT 0,
                status      TEXT DEFAULT 'draft',
                created     TEXT DEFAULT (datetime('now')),
                received_at TEXT
            )""")

        # 4) Communication Hub (unified timeline) --------------------------
        c.execute("""
            CREATE TABLE IF NOT EXISTS bo_comm_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                slug          TEXT NOT NULL,
                patient_phone TEXT DEFAULT '',
                channel       TEXT DEFAULT 'sms',
                direction     TEXT DEFAULT 'out',
                subject       TEXT DEFAULT '',
                body          TEXT DEFAULT '',
                status        TEXT DEFAULT 'sent',
                created       TEXT DEFAULT (datetime('now'))
            )""")

        # 6) Obsidian-style knowledge graph --------------------------------
        c.execute("""
            CREATE TABLE IF NOT EXISTS bo_links (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                slug      TEXT NOT NULL,
                from_type TEXT NOT NULL,
                from_id   TEXT NOT NULL,
                to_type   TEXT NOT NULL,
                to_id     TEXT NOT NULL,
                rel       TEXT DEFAULT 'related',
                created   TEXT DEFAULT (datetime('now')),
                UNIQUE(slug, from_type, from_id, to_type, to_id, rel)
            )""")

        # Back-office task queue (recall, reorder, χρεωστικά follow-up) -----
        c.execute("""
            CREATE TABLE IF NOT EXISTS bo_tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                slug         TEXT NOT NULL,
                title        TEXT DEFAULT '',
                kind         TEXT DEFAULT 'general',
                related_type TEXT DEFAULT '',
                related_id   TEXT DEFAULT '',
                due          TEXT DEFAULT '',
                priority     TEXT DEFAULT 'normal',
                status       TEXT DEFAULT 'open',
                created      TEXT DEFAULT (datetime('now'))
            )""")

        # Λίστα αναμονής — ποιον καλείς όταν ανοίξει κενή θέση (ακύρωση/no-show)
        c.execute("""
            CREATE TABLE IF NOT EXISTS bo_waitlist (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                slug      TEXT NOT NULL,
                name      TEXT DEFAULT '',
                phone     TEXT NOT NULL,
                service   TEXT DEFAULT '',
                note      TEXT DEFAULT '',
                status    TEXT DEFAULT 'waiting',
                created   TEXT DEFAULT (datetime('now'))
            )""")

        for idx in (
            "CREATE INDEX IF NOT EXISTS idx_bo_patients_slug ON bo_patients(slug)",
            "CREATE INDEX IF NOT EXISTS idx_bo_notes_pat ON bo_patient_notes(slug, patient_phone)",
            "CREATE INDEX IF NOT EXISTS idx_bo_inv_slug ON bo_invoices(slug, status)",
            "CREATE INDEX IF NOT EXISTS idx_bo_comm_pat ON bo_comm_log(slug, patient_phone)",
            "CREATE INDEX IF NOT EXISTS idx_bo_links_from ON bo_links(slug, from_type, from_id)",
            "CREATE INDEX IF NOT EXISTS idx_bo_links_to ON bo_links(slug, to_type, to_id)",
            "CREATE INDEX IF NOT EXISTS idx_bo_tasks_slug ON bo_tasks(slug, status)",
            "CREATE INDEX IF NOT EXISTS idx_bo_inv_inv ON bo_payments(slug, invoice_id)",
            "CREATE INDEX IF NOT EXISTS idx_bo_wait_slug ON bo_waitlist(slug, status)",
        ):
            c.execute(idx)

        # ── ΑΤΟΜΙΚΟΣ backstop: ένα τιμολόγιο ανά ραντεβού ────────────────────
        # Partial UNIQUE (booking_id IS NOT NULL) — αποτρέπει διπλά τιμολόγια για μία επίσκεψη υπό
        # ταυτόχρονη πίεση (BillingAgent vs autoinvoice_completed_bookings). Wrapped: αν υπάρχουν
        # παλιά duplicates από πριν, δεν σπάει το boot (ο app-level pre-check + IntegrityError-catch
        # στο create_invoice καλύπτουν τις νέες περιπτώσεις).
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bo_inv_booking "
                      "ON bo_invoices(slug, booking_id) WHERE booking_id IS NOT NULL")
        except Exception as _ie:
            print(f"[init_back_office] invoice unique index skipped (existing duplicates?): {_ie}")


# ════════════════════════════════════════════════════════════════════════════
# OBSIDIAN GRAPH — link/backlink
# ════════════════════════════════════════════════════════════════════════════
def link(slug, from_type, from_id, to_type, to_id, rel="related"):
    """Δημιουργεί edge στο graph (idempotent). Bidirectional στο query-time."""
    with _conn() as c:
        c.execute("""INSERT OR IGNORE INTO bo_links
                     (slug, from_type, from_id, to_type, to_id, rel)
                     VALUES (?,?,?,?,?,?)""",
                  (slug, from_type, str(from_id), to_type, str(to_id), rel))


def backlinks(slug, node_type, node_id):
    """Όλα τα συνδεδεμένα nodes (και προς τις δύο κατευθύνσεις) — το Obsidian backlink panel."""
    nid = str(node_id)
    with _conn() as c:
        out = c.execute("""SELECT to_type AS t, to_id AS i, rel FROM bo_links
                           WHERE slug=? AND from_type=? AND from_id=?""",
                        (slug, node_type, nid)).fetchall()
        inc = c.execute("""SELECT from_type AS t, from_id AS i, rel FROM bo_links
                           WHERE slug=? AND to_type=? AND to_id=?""",
                        (slug, node_type, nid)).fetchall()
    return [dict(r) for r in out] + [dict(r) for r in inc]


# ════════════════════════════════════════════════════════════════════════════
# 1) CRM AGENT — Ψηφιακός Φάκελος
# ════════════════════════════════════════════════════════════════════════════
def upsert_patient(slug, phone, name="", email="", **extra):
    """Δημιουργεί/ενημερώνει φάκελο ασθενή. Επιστρέφει το phone (natural key)."""
    if not phone:
        return None
    with _conn() as c:
        row = c.execute("SELECT id FROM bo_patients WHERE slug=? AND phone=?", (slug, phone)).fetchone()
        if row:
            sets, vals = ["updated=datetime('now')"], []
            if name:
                sets.append("name=?"); vals.append(name)
            if email:
                sets.append("email=?"); vals.append(email)
            for k in ("dob", "gender", "amka", "address", "occupation",
                      "referral_source", "medical_alerts", "status"):
                if extra.get(k):
                    # ΑΜΚΑ + ιατρικές ειδοποιήσεις = ευαίσθητα → encrypted at rest
                    v = encrypt(extra[k]) if k in ("medical_alerts", "amka") else extra[k]
                    sets.append(f"{k}=?"); vals.append(v)
            vals += [slug, phone]
            c.execute(f"UPDATE bo_patients SET {','.join(sets)} WHERE slug=? AND phone=?", vals)
        else:
            c.execute("""INSERT INTO bo_patients
                         (slug, phone, name, email, dob, gender, amka, address,
                          occupation, referral_source, medical_alerts, status)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (slug, phone, name, email, extra.get("dob", ""),
                       extra.get("gender", ""), encrypt(extra.get("amka", "")),
                       extra.get("address", ""), extra.get("occupation", ""),
                       extra.get("referral_source", ""),
                       encrypt(extra.get("medical_alerts", "")), extra.get("status", "active")))
    return phone


def sync_patients_from_bookings(slug):
    """CRM Agent executor: φτιάχνει/ενημερώνει φάκελο για ΚΑΘΕ ασθενή που έχει
    ραντεβού + linkάρει κάθε booking στον φάκελό του (Obsidian edge)."""
    created, linked = 0, 0
    with _conn() as c:
        rows = c.execute("""SELECT id, name, phone, email, date FROM bookings
                            WHERE slug=? AND phone!='' ORDER BY date""", (slug,)).fetchall()
    seen = {}
    for b in rows:
        ph = b["phone"]
        if ph not in seen:
            existed = upsert_patient(slug, ph, b["name"] or "", b["email"] or "")
            seen[ph] = True
            created += 1
        link(slug, "patient", ph, "booking", b["id"], "appointment")
        linked += 1
    # visit_count / last_visit refresh
    with _conn() as c:
        for ph in seen:
            agg = c.execute("""SELECT COUNT(*) n, MAX(date) last FROM bookings
                               WHERE slug=? AND phone=? AND status IN ('completed','confirmed')""",
                            (slug, ph)).fetchone()
            c.execute("UPDATE bo_patients SET updated=datetime('now') WHERE slug=? AND phone=?", (slug, ph))
    return {"patients": len(seen), "links": linked}


def _clean_phone(raw):
    """Κανονικοποίηση τηλεφώνου: κράτα ψηφία (+ προαιρετικό leading +)."""
    s = (raw or "").strip()
    if not s:
        return ""
    plus = s.startswith("+")
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    return ("+" if plus else "") + digits


def _norm_date(raw):
    """Δέξου YYYY-MM-DD ή DD/MM/YYYY ή DD-MM-YYYY → YYYY-MM-DD. Αλλιώς ''."""
    s = (raw or "").strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def import_patients(slug, rows, default_service=None):
    """Bulk import της ΥΠΑΡΧΟΥΣΑΣ πελατειακής βάσης του γιατρού (onboarding πραγματικού πελάτη).

    Κάθε row: {name, phone, email?, last_visit?}.  last_visit = YYYY-MM-DD ή DD/MM/YYYY.

    Γράφει ΔΥΟ πράγματα γιατί το win-back/recovery διαβάζει το `bookings`, ΟΧΙ το bo_patients:
      1. Φάκελο ασθενή (bo_patients)               → CRM, αναζήτηση, σημειώσεις.
      2. completed booking [IMPORTED] στο last_visit → ΕΝΕΡΓΟΠΟΙΕΙ το win-back (dormant detection).
    Idempotent: ίδιο phone δεν διπλασιάζεται· ίδιο [IMPORTED] booking (phone+date) δεν ξαναμπαίνει.

    Επιστρέφει {imported, with_history, dormant_90} — dormant_90 = πόσοι είναι win-back-able ΤΩΡΑ.
    """
    biz = get_business(slug) or {}
    svc0 = default_service
    if not svc0 and biz.get("services"):
        svc0 = ((biz["services"] or [{}])[0] or {}).get("name")
    svc0 = svc0 or "Συνεδρία"
    dur = int(biz.get("slot_minutes") or 30)
    cutoff90 = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    imported = with_hist = dormant = 0
    seen = set()
    history = []  # (phone, name, email, last_visit)
    for r in rows:
        phone = _clean_phone(r.get("phone"))
        if not phone or phone in seen:
            continue
        seen.add(phone)
        name = (r.get("name") or "").strip()[:120]
        email = (r.get("email") or "").strip()[:200]
        upsert_patient(slug, phone, name, email)
        imported += 1
        lv = _norm_date(r.get("last_visit"))
        if lv:
            history.append((phone, name, email, lv))
    # ιστορικό-bookings σε ένα batch (το win-back ψάχνει εδώ)
    with _conn() as c:
        for phone, name, email, lv in history:
            ex = c.execute("SELECT 1 FROM bookings WHERE slug=? AND phone=? AND date=? "
                           "AND notes LIKE '%[IMPORTED]%'", (slug, phone, lv)).fetchone()
            if not ex:
                c.execute("INSERT INTO bookings (slug,date,time,service,duration,name,phone,email,"
                          "status,notes,reminder_24,reminder_4) VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
                          (slug, lv, "10:00", svc0, dur, name, phone, email, "completed", "[IMPORTED]"))
            with_hist += 1
            if lv <= cutoff90:
                dormant += 1
        c.commit()
    return {"imported": imported, "with_history": with_hist, "dormant_90": dormant}


def add_patient_note(slug, phone, body, kind="clinical", title="", author="γιατρός", booking_id=None):
    # Κλινικό περιεχόμενο (διάγνωση/πλάνο) → encrypted at rest (GDPR Art.9/32)
    body_enc = encrypt(body) if kind == "clinical" else body
    with _conn() as c:
        cur = c.execute("""INSERT INTO bo_patient_notes
                           (slug, patient_phone, kind, title, body, author, booking_id)
                           VALUES (?,?,?,?,?,?,?)""",
                        (slug, phone, kind, title, body_enc, author, booking_id))
        nid = cur.lastrowid
    link(slug, "patient", phone, "note", nid, "record")
    if booking_id:
        link(slug, "booking", booking_id, "note", nid, "documented")
    return nid


def patient_360(slug, phone):
    """Πλήρης φάκελος 'σαν Obsidian': profile + ραντεβού + τιμολόγια + επικοινωνία
    + σημειώσεις + έγγραφα + όλα τα backlinks. Ένα view, τα πάντα συνδεδεμένα."""
    with _conn() as c:
        prof = c.execute("SELECT * FROM bo_patients WHERE slug=? AND phone=?", (slug, phone)).fetchone()
        appts = c.execute("""SELECT id, date, time, service, status FROM bookings
                             WHERE slug=? AND phone=? ORDER BY date DESC, time DESC LIMIT 50""",
                          (slug, phone)).fetchall()
        invs = c.execute("""SELECT id, number, issue_date, service, total, paid, status
                            FROM bo_invoices WHERE slug=? AND patient_phone=? ORDER BY issue_date DESC""",
                         (slug, phone)).fetchall()
        comms = c.execute("""SELECT channel, direction, subject, body, status, created
                             FROM bo_comm_log WHERE slug=? AND patient_phone=?
                             ORDER BY created DESC LIMIT 50""", (slug, phone)).fetchall()
        notes = c.execute("""SELECT id, kind, title, body, author, created
                             FROM bo_patient_notes WHERE slug=? AND patient_phone=?
                             ORDER BY created DESC""", (slug, phone)).fetchall()
        docs = c.execute("""SELECT id, title, doc_type, ref, created FROM bo_documents
                            WHERE slug=? AND patient_phone=? ORDER BY created DESC""",
                         (slug, phone)).fetchall()
    balance = sum(max((i["total"] or 0) - (i["paid"] or 0), 0) for i in invs)
    prof_d = dict(prof) if prof else {"phone": phone, "name": ""}
    # decrypt ευαίσθητα πεδία για προβολή στον εξουσιοδοτημένο γιατρό
    if prof_d.get("medical_alerts"):
        prof_d["medical_alerts"] = decrypt(prof_d["medical_alerts"])
    if prof_d.get("amka"):
        prof_d["amka"] = decrypt(prof_d["amka"])
    notes_d = []
    for r in notes:
        nd = dict(r)
        nd["body"] = decrypt(nd.get("body"))
        notes_d.append(nd)
    return {
        "profile": prof_d,
        "appointments": [dict(r) for r in appts],
        "invoices": [dict(r) for r in invs],
        "communications": [dict(r) for r in comms],
        "notes": notes_d,
        "documents": [dict(r) for r in docs],
        "balance": round(balance, 2),
        "graph": backlinks(slug, "patient", phone),
    }


# ════════════════════════════════════════════════════════════════════════════
# 3) FINANCE AGENT — Τιμολόγια / Υπόλοιπα / myDATA
# ════════════════════════════════════════════════════════════════════════════
def _next_invoice_number(c, slug):
    n = c.execute("SELECT COUNT(*) FROM bo_invoices WHERE slug=?", (slug,)).fetchone()[0] or 0
    yr = datetime.now().strftime("%Y")
    return f"{yr}-{n + 1:04d}"


def create_invoice(slug, patient_phone, patient_name, service, amount,
                   vat_rate=24, booking_id=None, status="unpaid"):
    """Εκδίδει τιμολόγιο (gross→net+ΦΠΑ) έτοιμο για myDATA. Linkάρει σε ασθενή+ραντεβού."""
    amount = round(float(amount), 2)
    vat = round(amount * vat_rate / (100 + vat_rate), 2) if vat_rate else 0.0  # amount = gross
    net = round(amount - vat, 2)
    mark = "Απαλλαγή ΦΠΑ — Άρθρο 22 ν.2859/2000" if not vat_rate else ""
    with _conn() as c:
        # IDEMPOTENT: ένα τιμολόγιο ανά ραντεβού. Δύο paths (BillingAgent + autoinvoice_completed_bookings)
        # μπορεί να τρέξουν για το ίδιο booking· ο pre-check + το UNIQUE(slug,booking_id) index (init) +
        # το IntegrityError-catch εγγυώνται ότι δεν εκδίδονται 2 τιμολόγια για μία επίσκεψη.
        if booking_id is not None:
            ex = c.execute("SELECT id FROM bo_invoices WHERE slug=? AND booking_id=? LIMIT 1",
                           (slug, booking_id)).fetchone()
            if ex:
                return ex["id"]
        num = _next_invoice_number(c, slug)
        due = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        try:
            cur = c.execute("""INSERT INTO bo_invoices
                               (slug, number, patient_phone, patient_name, due_date,
                                service, amount, vat_rate, vat, total, status, mydata_mark, booking_id)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (slug, num, patient_phone, patient_name, due, service,
                             net, vat_rate, vat, amount, status, mark, booking_id))
            iid = cur.lastrowid
        except sqlite3.IntegrityError:
            # race: άλλο thread εξέδωσε τιμολόγιο για το ίδιο booking μεταξύ check & insert
            row = c.execute("SELECT id FROM bo_invoices WHERE slug=? AND booking_id=? LIMIT 1",
                            (slug, booking_id)).fetchone()
            if row:
                return row["id"]
            raise
    if patient_phone:
        link(slug, "patient", patient_phone, "invoice", iid, "billed")
    if booking_id:
        link(slug, "booking", booking_id, "invoice", iid, "invoiced")
    return iid


def record_payment(slug, invoice_id, amount, method="cash"):
    """Καταχωρεί πληρωμή. Reject ≤0, cap στο υπόλοιπο (no overpay), idempotent
    (μπλοκάρει διπλή πληρωμή/double-click). Read+write σε ένα connection (atomic)."""
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Μη έγκυρο ποσό"}
    if amount <= 0:
        return {"ok": False, "error": "Το ποσό πρέπει να είναι θετικό"}
    with _conn() as c:
        inv = c.execute("SELECT total, paid FROM bo_invoices WHERE id=? AND slug=?",
                        (int(invoice_id), slug)).fetchone()
        if not inv:
            return {"ok": False, "error": "Το τιμολόγιο δεν βρέθηκε"}
        total = inv["total"] or 0
        already = inv["paid"] or 0
        remaining = round(total - already, 2)
        if remaining <= 0.01:
            return {"ok": False, "error": "Ήδη εξοφλημένο"}
        applied = min(amount, remaining)              # no overpay
        new_paid = round(already + applied, 2)
        st = "paid" if new_paid >= total - 0.01 else "partial"
        c.execute("INSERT INTO bo_payments (slug, invoice_id, amount, method) VALUES (?,?,?,?)",
                  (slug, int(invoice_id), applied, method))
        c.execute("UPDATE bo_invoices SET paid=?, status=? WHERE id=? AND slug=?",
                  (new_paid, st, int(invoice_id), slug))
    return {"ok": True, "applied": applied}


def autoinvoice_completed_bookings(slug):
    """Finance Agent executor: εκδίδει αυτόματα τιμολόγιο για κάθε ΟΛΟΚΛΗΡΩΜΕΝΟ
    ραντεβού που δεν έχει ήδη τιμολόγιο. Τιμή = από τις υπηρεσίες της επιχείρησης.
    ΦΠΑ: 0% για ιατρικές υπηρεσίες (απαλλαγή Άρθρο 22 ν.2859/2000), 24% για αισθητικά."""
    from vertical_templates import vat_rate_for
    issued = 0
    with _conn() as c:
        biz = c.execute("SELECT type, services FROM businesses WHERE slug=?", (slug,)).fetchone()
        try:
            svc_prices = {s.get("name"): float(s.get("price", 0))
                          for s in json.loads(biz["services"] or "[]")} if biz else {}
        except Exception:
            svc_prices = {}
        done = c.execute("""SELECT b.id, b.name, b.phone, b.service FROM bookings b
                            WHERE b.slug=? AND b.status='completed'
                            AND NOT EXISTS (SELECT 1 FROM bo_invoices i
                                            WHERE i.slug=b.slug AND i.booking_id=b.id)""",
                         (slug,)).fetchall()
    vat = vat_rate_for(biz["type"] if biz else "")
    for b in done:
        price = svc_prices.get(b["service"], 0) or 40  # fallback μέση τιμή
        create_invoice(slug, b["phone"], b["name"], b["service"], price,
                       vat_rate=vat, booking_id=b["id"], status="unpaid")
        issued += 1
    return {"invoices_issued": issued, "vat_rate": vat}


def outstanding_balances(slug):
    """Όλα τα ανοιχτά υπόλοιπα ανά ασθενή — το 'ποιος μου χρωστάει'."""
    with _conn() as c:
        rows = c.execute("""SELECT patient_phone, patient_name,
                                   SUM(MAX(total - paid, 0)) AS owed, COUNT(*) AS n
                            FROM bo_invoices WHERE slug=? AND status!='paid'
                            GROUP BY patient_phone HAVING owed > 0.01
                            ORDER BY owed DESC""", (slug,)).fetchall()
    return [dict(r) for r in rows]


def add_expense(slug, category, vendor, amount, vat=0, note="", expense_date=None):
    with _conn() as c:
        c.execute("""INSERT INTO bo_expenses (slug, category, vendor, amount, vat, note, expense_date)
                     VALUES (?,?,?,?,?,?,COALESCE(?,date('now')))""",
                  (slug, category, vendor, round(float(amount), 2), vat, note, expense_date))


def finance_summary(slug, days=30):
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _conn() as c:
        rev = c.execute("SELECT COALESCE(SUM(paid),0) FROM bo_invoices WHERE slug=? AND issue_date>=?",
                        (slug, since)).fetchone()[0]
        billed = c.execute("SELECT COALESCE(SUM(total),0) FROM bo_invoices WHERE slug=? AND issue_date>=?",
                           (slug, since)).fetchone()[0]
        outstanding = c.execute("SELECT COALESCE(SUM(total-paid),0) FROM bo_invoices WHERE slug=? AND status!='paid'",
                                (slug,)).fetchone()[0]
        exp = c.execute("SELECT COALESCE(SUM(amount),0) FROM bo_expenses WHERE slug=? AND expense_date>=?",
                        (slug, since)).fetchone()[0]
        mydata_pending = c.execute("SELECT COUNT(*) FROM bo_invoices WHERE slug=? AND mydata_status='pending'",
                                   (slug,)).fetchone()[0]
    return {
        "revenue": round(rev or 0, 2), "billed": round(billed or 0, 2),
        "outstanding": round(outstanding or 0, 2), "expenses": round(exp or 0, 2),
        "profit": round((rev or 0) - (exp or 0), 2), "mydata_pending": mydata_pending,
    }


# ════════════════════════════════════════════════════════════════════════════
# 4) COMMUNICATION HUB — unified timeline + recall
# ════════════════════════════════════════════════════════════════════════════
def log_comm(slug, patient_phone, channel, body, direction="out", subject="", status="sent"):
    with _conn() as c:
        cur = c.execute("""INSERT INTO bo_comm_log
                           (slug, patient_phone, channel, direction, subject, body, status)
                           VALUES (?,?,?,?,?,?,?)""",
                        (slug, patient_phone, channel, direction, subject, body, status))
        cid = cur.lastrowid
    if patient_phone:
        link(slug, "patient", patient_phone, "comm", cid, "contacted")
    return cid


def recall_due(slug, days_inactive=None):
    """Communication Hub executor: ασθενείς που πρέπει να γίνει recall (vertical-aware)."""
    from vertical_templates import resolve_vertical, winback_days
    with _conn() as c:
        biz = c.execute("SELECT type FROM businesses WHERE slug=?", (slug,)).fetchone()
    vert = resolve_vertical(biz["type"] if biz else "")
    days = days_inactive or winback_days(vert)
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _conn() as c:
        rows = c.execute("""SELECT phone, name, MAX(date) AS last FROM bookings
                            WHERE slug=? AND status IN ('completed','confirmed')
                            GROUP BY phone HAVING last < ? ORDER BY last""",
                         (slug, cutoff)).fetchall()
    return {"days": days, "vertical": vert, "due": [dict(r) for r in rows]}


def money_on_table(slug):
    """€ «στο τραπέζι ΤΩΡΑ» — το hook που δικαιολογεί τη συνδρομή (Stripe-style «money at risk»):
    χρωστούμενα (σίγουρα οφειλόμενα) + αδρανείς ασθενείς (πιθανή επόμενη συνεδρία, conservative 1/άτομο).
    Επιστρέφει {at_risk_eur, outstanding_eur, dormant_eur, dormant_count, debtor_count, avg_session, top}."""
    biz = get_business(slug) or {}
    prices = []
    for s in (biz.get("services") or []):
        if isinstance(s, dict):
            try:
                p = float(s.get("price") or 0)
                if p > 0:
                    prices.append(p)
            except (TypeError, ValueError):
                pass
    avg = round(sum(prices) / len(prices), 2) if prices else 40.0
    debtors = outstanding_balances(slug)
    outstanding = round(sum(float(d.get("owed") or 0) for d in debtors), 2)
    dormant = recall_due(slug).get("due", [])
    dormant_count = len(dormant)
    dormant_eur = round(dormant_count * avg, 2)   # 1 συνεδρία/αδρανή — undersell ώστε να είναι πιστευτό
    top = [{"name": d.get("patient_name") or "—", "reason": "χρωστάει",
            "eur": round(float(d.get("owed") or 0), 2)} for d in debtors[:3]]
    top += [{"name": p.get("name") or "—", "reason": "αδρανής", "eur": avg} for p in dormant[:3]]
    return {"at_risk_eur": round(outstanding + dormant_eur, 2),
            "outstanding_eur": outstanding, "dormant_eur": dormant_eur,
            "dormant_count": dormant_count, "debtor_count": len(debtors),
            "avg_session": avg, "top": top[:5]}


# ════════════════════════════════════════════════════════════════════════════
# 5) INVENTORY / SUPPLIERS AGENT
# ════════════════════════════════════════════════════════════════════════════
def add_supplier(slug, name, category="", contact="", phone="", email="", notes=""):
    with _conn() as c:
        cur = c.execute("""INSERT INTO bo_suppliers (slug, name, category, contact, phone, email, notes)
                           VALUES (?,?,?,?,?,?,?)""", (slug, name, category, contact, phone, email, notes))
        return cur.lastrowid


def add_inventory_item(slug, name, qty=0, reorder_level=0, unit="τεμ",
                       unit_cost=0, category="", sku="", supplier_id=None):
    with _conn() as c:
        cur = c.execute("""INSERT INTO bo_inventory
                           (slug, sku, name, category, qty, unit, reorder_level, unit_cost, supplier_id)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (slug, sku, name, category, qty, unit, reorder_level, unit_cost, supplier_id))
        iid = cur.lastrowid
    if supplier_id:
        link(slug, "supplier", supplier_id, "inventory", iid, "supplies")
    return iid


def _send_patient_message(slug, phone, name, text, subject="Ενημέρωση"):
    """Best-effort μήνυμα σε ασθενή (email→SMS). True αν έφυγε, αλλιώς False (καταγράφεται queued)."""
    if not phone:
        return False
    email = ""
    with _conn() as c:
        row = c.execute("SELECT email FROM bo_patients WHERE slug=? AND phone=?", (slug, phone)).fetchone()
        email = (row["email"] if row else "") or ""
    if email:
        try:
            from email_service import send_email_via_resend
            biz = get_business(slug)
            fn = biz["name"] if biz else "Ιατρείο"
            if send_email_via_resend(email, subject, f"<p>{text}</p>", from_name=fn):
                log_comm(slug, phone, "email", text, direction="out", subject=subject, status="sent")
                return True
        except Exception as e:
            print(f"[money-action email] {e}")
    try:
        from sms_service import send_sms
        if send_sms(phone, text):
            log_comm(slug, phone, "sms", text, direction="out", subject=subject, status="sent")
            return True
    except Exception as e:
        print(f"[money-action sms] {e}")
    return False


def money_actions(slug):
    """PROACTIVE «τι κάνω σήμερα για λεφτά» — έτοιμες, εκτελέσιμες κινήσεις ανάκτησης εσόδων.
    Αυτό είναι ο πυρήνας του προϊόντος: όχι «ρώτα με», αλλά «κάνε αυτά → πάρε €X πίσω»."""
    biz = get_business(slug) or {}
    prices = [float(s.get("price") or 0) for s in (biz.get("services") or [])
              if isinstance(s, dict) and (s.get("price") or 0)]
    avg = round(sum(prices) / len(prices), 2) if prices else 40.0
    actions = []
    # 1) Αδρανείς ασθενείς → win-back (το μεγάλο €)
    dormant = recall_due(slug).get("due", [])
    if dormant:
        actions.append({
            "key": "winback", "icon": "🔄",
            "title": "Φέρε πίσω χαμένους ασθενείς",
            "subtitle": f"{len(dormant)} ασθενείς δεν έχουν έρθει εδώ και καιρό",
            "count": len(dormant), "eur": round(len(dormant) * avg, 2),
            "who": [d.get("name") or "—" for d in dormant[:6]],
            "cta": "Στείλ' τους υπενθύμιση να ξανάρθουν",
        })
    # 2) Χρωστούμενα → υπενθύμιση πληρωμής (σίγουρα λεφτά)
    debtors = outstanding_balances(slug)
    if debtors:
        owed = round(sum(float(d.get("owed") or 0) for d in debtors), 2)
        actions.append({
            "key": "collect", "icon": "💶",
            "title": "Ζήτα τα χρωστούμενα",
            "subtitle": f"{len(debtors)} ασθενείς χρωστάνε συνολικά",
            "count": len(debtors), "eur": owed,
            "who": [f"{d.get('patient_name') or '—'} — €{round(float(d.get('owed') or 0))}" for d in debtors[:6]],
            "cta": "Στείλε ευγενική υπενθύμιση πληρωμής",
        })
    rec = recovered_revenue(slug, days=30)
    return {
        "actions": actions,
        "total_at_risk_eur": round(sum(a["eur"] for a in actions), 2),
        "recovered_30d_eur": rec.get("recovered_eur", 0),
        "recovered_30d_count": rec.get("recovered_count", 0),
    }


def run_money_action(slug, key):
    """Εκτελεί ΜΙΑ money action με ένα κλικ. winback → επανακλήσεις· collect → υπενθυμίσεις πληρωμής."""
    if key == "winback":
        try:
            from voice.outbound import enqueue_winback_for_inactive_customers
            n = enqueue_winback_for_inactive_customers(slug, limit=50)
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}
        return {"ok": True, "count": n,
                "msg": (f"Ξεκίνησε επανάκληση σε {n} χαμένους ασθενείς — θα τους φέρουμε πίσω." if n
                        else "Κανείς ασθενής δεν πληροί τα κριτήρια αυτή τη στιγμή.")}
    if key == "collect":
        debtors = outstanding_balances(slug)
        sent = 0
        for d in debtors:
            phone = d.get("patient_phone")
            name = d.get("patient_name") or ""
            owed = round(float(d.get("owed") or 0))
            text = (f"Καλησπέρα{(' ' + name) if name else ''}, μια ευγενική υπενθύμιση ότι εκκρεμεί "
                    f"πληρωμή €{owed} στο ιατρείο μας. Ευχαριστούμε πολύ!")
            if _send_patient_message(slug, phone, name, text, subject="Υπενθύμιση πληρωμής"):
                sent += 1
            else:
                log_comm(slug, phone, "note", text, direction="out",
                         subject="Υπενθύμιση πληρωμής", status="queued")
        if sent:
            return {"ok": True, "count": sent, "msg": f"Στάλθηκαν {sent} υπενθυμίσεις πληρωμής."}
        return {"ok": True, "count": 0,
                "msg": f"Ετοιμάστηκαν {len(debtors)} υπενθυμίσεις — θα φύγουν μόλις συνδέσεις email ή SMS."}
    return {"ok": False, "error": "Άγνωστη ενέργεια"}


def auto_payment_reminders(slug, cooldown_days=7):
    """ΑΥΤΟΜΑΤΗ ευγενική υπενθύμιση πληρωμής σε όσους χρωστάνε — μία φορά ανά cooldown_days
    (dedup μέσω bo_comm_log → μηδέν spam). Καλείται καθημερινά από scheduler.
    Έτσι το follow-up πληρωμών γίνεται μόνο του, όχι μόνο με κουμπί."""
    biz = get_business(slug) or {}
    fn = biz.get("name") or "το ιατρείο"
    cutoff = (datetime.now() - timedelta(days=cooldown_days)).strftime("%Y-%m-%d %H:%M:%S")
    sent = queued = 0
    for d in outstanding_balances(slug):
        phone = d.get("patient_phone")
        if not phone:
            continue
        owed = round(float(d.get("owed") or 0))
        if owed < 1:
            continue
        with _conn() as c:
            recent = c.execute(
                "SELECT 1 FROM bo_comm_log WHERE slug=? AND patient_phone=? "
                "AND subject='Υπενθύμιση πληρωμής' AND created>=? LIMIT 1",
                (slug, phone, cutoff)).fetchone()
        if recent:
            continue  # υπενθυμίστηκε πρόσφατα — μην ξαναενοχλείς
        name = d.get("patient_name") or ""
        first = name.split(" ")[0] if name else ""
        text = (f"Καλησπέρα{(' ' + first) if first else ''}, μια ευγενική υπενθύμιση ότι "
                f"εκκρεμεί πληρωμή €{owed} στο {fn}. Ευχαριστούμε πολύ!")
        if _send_patient_message(slug, phone, name, text, subject="Υπενθύμιση πληρωμής"):
            sent += 1
        else:
            log_comm(slug, phone, "note", text, direction="out",
                     subject="Υπενθύμιση πληρωμής", status="queued")
            queued += 1
    return {"reminded": sent, "queued": queued}


def automation_activity(slug, days=7):
    """ΤΙ ΕΚΑΝΕ Ο AI ΒΟΗΘΟΣ ΜΟΝΟΣ ΤΟΥ τις τελευταίες <days> μέρες.
    Ο πυρήνας του «δίνει αξία, δεν είναι απλά dashboard»: κάνει visible τη δουλειά που
    τρέχει αυτόνομα 24/7 (win-back, recovery, reminders, τιμολόγια, ενημερώσεις).
    Διαβάζει ΜΟΝΟ πραγματικά γεγονότα από τη βάση — μηδέν fake νούμερα."""
    since_dt = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    since_d = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _conn() as c:
        try:
            jobs = c.execute("""SELECT job_type, COUNT(*) AS n FROM outbound_jobs
                                WHERE slug=? AND status='done'
                                AND COALESCE(processed_at, created) >= ?
                                GROUP BY job_type""", (slug, since_dt)).fetchall()
        except Exception:
            jobs = []
        reminders = c.execute("""SELECT COUNT(*) FROM bookings WHERE slug=?
                                 AND (reminder_24=1 OR reminder_4=1) AND date >= ?""",
                              (slug, since_d)).fetchone()[0]
        reviews = c.execute("""SELECT COUNT(*) FROM bookings WHERE slug=?
                               AND review_sent=1 AND date >= ?""", (slug, since_d)).fetchone()[0]
        invoices = c.execute("""SELECT COUNT(*) FROM bo_invoices WHERE slug=? AND created >= ?""",
                             (slug, since_dt)).fetchone()[0]
    jm = {r["job_type"]: r["n"] for r in jobs}
    items = []
    def add(icon, n, label):
        if n:
            items.append({"icon": icon, "n": int(n), "label": label})
    add("📞", jm.get("win_back", 0), "win-back σε χαμένους ασθενείς")
    add("🔄", jm.get("no_show_recovery", 0), "επαναφορές μετά από no-show")
    add("⏰", jm.get("no_show_reminder", 0), "υπενθυμίσεις λίγο πριν το ραντεβού")
    add("📨", reminders, "υπενθυμίσεις ραντεβού (24ω/4ω)")
    add("⭐", reviews, "αιτήματα κριτικής Google")
    add("🧾", invoices, "τιμολόγια εκδόθηκαν αυτόματα")
    add("☀️", jm.get("owner_briefing", 0), "ημερήσιες ενημερώσεις σε σένα")
    rec = recovered_revenue(slug, days)
    return {
        "days": days, "items": items,
        "total_actions": sum(i["n"] for i in items),
        "recovered_eur": rec.get("recovered_eur", 0),
        "recovered_count": rec.get("recovered_count", 0),
    }


def low_stock(slug):
    """Inventory Agent executor: τι πρέπει να παραγγείλεις (qty <= reorder_level)."""
    with _conn() as c:
        rows = c.execute("""SELECT i.id, i.name, i.qty, i.unit, i.reorder_level, i.unit_cost,
                                   s.name AS supplier FROM bo_inventory i
                            LEFT JOIN bo_suppliers s ON s.id=i.supplier_id AND s.slug=i.slug
                            WHERE i.slug=? AND i.qty <= i.reorder_level ORDER BY i.qty""",
                         (slug,)).fetchall()
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
# TASKS (recall / reorder / χρεωστικά follow-up) — ό,τι "πρέπει να γίνει"
# ════════════════════════════════════════════════════════════════════════════
def add_task(slug, title, kind="general", related_type="", related_id="", due="", priority="normal"):
    with _conn() as c:
        c.execute("""INSERT INTO bo_tasks (slug, title, kind, related_type, related_id, due, priority)
                     VALUES (?,?,?,?,?,?,?)""",
                  (slug, title, kind, related_type, str(related_id), due, priority))


def complete_task(slug, task_id):
    """Μαρκάρει εργασία ως ολοκληρωμένη (owner action). False αν το task_id δεν υπάρχει
    (ή ανήκει σε άλλο slug) — ώστε το route να μη λέει 'ok' σε κάτι που δεν έγινε."""
    with _conn() as c:
        cur = c.execute("UPDATE bo_tasks SET status='done' WHERE slug=? AND id=?", (slug, int(task_id)))
        return cur.rowcount > 0


def rebuild_tasks(slug):
    """Συνθέτει το 'to-do της γραμματείας' από όλα τα agents: recall + reorder + χρεωστικά."""
    with _conn() as c:
        c.execute("DELETE FROM bo_tasks WHERE slug=? AND status='open' AND kind IN ('recall','reorder','balance')", (slug,))
    n = 0
    for r in recall_due(slug)["due"][:50]:
        add_task(slug, f"Recall: {r['name'] or r['phone']} (τελευταία {r['last']})",
                 kind="recall", related_type="patient", related_id=r["phone"], priority="normal")
        n += 1
    for it in low_stock(slug):
        add_task(slug, f"Παραγγελία: {it['name']} (απόθεμα {it['qty']}{it['unit']})",
                 kind="reorder", related_type="inventory", related_id=it["id"], priority="high")
        n += 1
    for b in outstanding_balances(slug)[:50]:
        add_task(slug, f"Χρεωστικό: {b['patient_name'] or b['patient_phone']} οφείλει €{round(b['owed'],2)}",
                 kind="balance", related_type="patient", related_id=b["patient_phone"], priority="high")
        n += 1
    return {"tasks": n}


# ════════════════════════════════════════════════════════════════════════════
# CALENDAR OPTIMIZER — chair-time / κενά
# ════════════════════════════════════════════════════════════════════════════
def calendar_utilization(slug, day=None):
    """Chair-time utilization μιας ημέρας: λεπτά κρατημένα vs διαθέσιμα + κενά slots."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        biz = c.execute("SELECT day_start, day_end, slot_minutes FROM businesses WHERE slug=?", (slug,)).fetchone()
        appts = c.execute("""SELECT time, duration, service, name FROM bookings
                             WHERE slug=? AND date=? AND status IN ('confirmed','completed','pending_outcome')
                             ORDER BY time""", (slug, day)).fetchall()
    ds = (biz["day_start"] if biz else "09:00") or "09:00"
    de = (biz["day_end"] if biz else "21:00") or "21:00"
    def _m(t):
        h, mm = (t.split(":") + ["0"])[:2]
        return int(h) * 60 + int(mm)
    open_min = max(_m(de) - _m(ds), 1)
    booked = sum((a["duration"] or 30) for a in appts)
    util = round(min(booked / open_min * 100, 100), 1)
    return {
        "date": day, "open_minutes": open_min, "booked_minutes": booked,
        "utilization_pct": util, "appointments": len(appts),
        "free_minutes": max(open_min - booked, 0),
    }


# ════════════════════════════════════════════════════════════════════════════
# DAY BOARD — η καθημερινή οθόνη της γραμματείας (check-in / ταμείο / αυριανά)
# ════════════════════════════════════════════════════════════════════════════
def _date_shift(day, delta):
    try:
        return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=delta)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return day


def _service_prices(slug):
    """{όνομα υπηρεσίας: τιμή} από τις ρυθμίσεις της επιχείρησης."""
    prices = {}
    for s in ((get_business(slug) or {}).get("services") or []):
        if isinstance(s, dict) and s.get("name"):
            try:
                prices[s["name"]] = float(s.get("price") or 0)
            except (TypeError, ValueError):
                pass
    return prices


def day_board(slug, day=None):
    """Ό,τι κοιτάει η γραμματεία ΟΛΗ μέρα: σημερινά ραντεβού με κουμπιά «ήρθε/δεν ήρθε»,
    το ταμείο της ημέρας (τι εισπράχθηκε), και πόσα αυριανά μένουν να επιβεβαιωθούν.
    Επιστρέφει τα πάντα σε ένα call ώστε το UI να μη χτυπάει πολλά endpoints."""
    today = datetime.now().strftime("%Y-%m-%d")
    day = day or today
    prices = _service_prices(slug)
    tomorrow = _date_shift(day, 1)
    with _conn() as c:
        appts = c.execute("""SELECT id, time, service, name, phone, status, duration, notes, created
                             FROM bookings WHERE slug=? AND date=?
                             AND status IN ('confirmed','pending_outcome','completed','no_show')
                             ORDER BY time""", (slug, day)).fetchall()
        pays = c.execute("""SELECT method, COALESCE(SUM(amount),0) AS amt, COUNT(*) AS n
                            FROM bo_payments WHERE slug=? AND date(paid_at)=?
                            GROUP BY method""", (slug, day)).fetchall()
        tmrw_confirm = c.execute("""SELECT COUNT(*) FROM bookings WHERE slug=? AND date=?
                                    AND status='confirmed' AND reminder_24=0""",
                                 (slug, tomorrow)).fetchone()[0]
        tmrw_total = c.execute("""SELECT COUNT(*) FROM bookings WHERE slug=? AND date=?
                                  AND status='confirmed'""", (slug, tomorrow)).fetchone()[0]
    rows = []
    # created = UTC (SQLite CURRENT_TIMESTAMP) → σύγκρινε με utcnow, ΟΧΙ local now (διαφορά +3h)
    _utcnow = datetime.utcnow()
    for a in appts:
        d = dict(a)
        d["price"] = prices.get(a["service"], 0)
        d["resolved"] = a["status"] in ("completed", "no_show")
        # ── Real-time Vapi link: ξεχώρισε τις τηλεφωνικές κρατήσεις του AI ─────
        notes = (a["notes"] or "")
        d["source"] = "voice" if "[VOICE]" in notes else "manual"
        # «μόλις τώρα» (τελευταία 10 λεπτά) → highlight «ο βοηθός μόλις έκλεισε»
        d["fresh"] = False
        if d["source"] == "voice" and a["created"]:
            try:
                ct = datetime.fromisoformat(str(a["created"]).replace("Z", "").replace("T", " ").split(".")[0])
                d["fresh"] = 0 <= (_utcnow - ct).total_seconds() < 600
            except Exception:
                pass
        d.pop("notes", None)  # μην εκθέτουμε raw notes στο UI
        rows.append(d)
    by_method = {p["method"]: round(p["amt"] or 0, 2) for p in pays}
    collected = round(sum(p["amt"] or 0 for p in pays), 2)
    counts = {
        "total": len(rows),
        "done": sum(1 for r in rows if r["status"] == "completed"),
        "no_show": sum(1 for r in rows if r["status"] == "no_show"),
        "pending": sum(1 for r in rows if not r["resolved"]),
    }
    # «αναμενόμενο ταμείο» = αξία όσων ολοκληρώθηκαν (σίγουρα) + όσων μένουν (confirmed).
    # ΟΧΙ pending_outcome (αβέβαιο — ίσως no-show) ώστε να μην overstate-άρει το ταμείο.
    expected = round(sum(r["price"] for r in rows
                         if r["status"] in ("completed", "confirmed")), 2)
    counts["voice"] = sum(1 for r in rows if r["source"] == "voice")
    # feed «η Ράνια μόλις έκλεισε»: φρέσκες τηλεφωνικές κρατήσεις (πιο πρόσφατες πρώτα)
    voice_feed = [{"name": r["name"], "time": r["time"], "service": r["service"]}
                  for r in rows if r.get("fresh")]
    return {
        "date": day, "prev": _date_shift(day, -1), "next": _date_shift(day, 1),
        "is_today": day == today,
        "appointments": rows, "counts": counts,
        "till": {"collected": collected, "by_method": by_method, "expected": expected},
        "tomorrow": {"date": tomorrow, "to_confirm": tmrw_confirm, "total": tmrw_total},
        "voice_feed": voice_feed,
    }


def resolve_booking(slug, booking_id, outcome):
    """Η γραμματεία πατάει «✓ Ήρθε» (completed) ή «✕ Δεν ήρθε» (no_show).
    completed → εκδίδει αυτόματα τιμολόγιο (idempotent) ώστε να μπει στο ταμείο/οικονομικά.
    no_show → ελευθερώνει τη θέση: το ίδιο closed-loop notify_waitlist (email+SMS+claim-link) που
    χρησιμοποιούν τα cancel endpoints, ώστε το «✕ Δεν ήρθε» κουμπί να γεμίζει τη θέση όπως ένα cancel."""
    try:
        bid = int(booking_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Μη έγκυρο ραντεβού"}
    if outcome not in ("completed", "no_show"):
        return {"ok": False, "error": "Μη έγκυρη ενέργεια"}
    # tenant isolation: το booking πρέπει να ανήκει στο slug
    with _conn() as c:
        own = c.execute("SELECT 1 FROM bookings WHERE id=? AND slug=?", (bid, slug)).fetchone()
    if not own:
        return {"ok": False, "error": "Το ραντεβού δεν βρέθηκε"}
    if not mark_booking_outcome(bid, outcome):
        return {"ok": False, "error": "Έχει ήδη οριστεί η έκβαση αυτού του ραντεβού"}
    if outcome == "completed":
        try:
            autoinvoice_completed_bookings(slug)   # idempotent — τιμολογεί μόνο ό,τι δεν έχει
        except Exception as e:
            print(f"[resolve_booking autoinvoice] {e}")
    elif outcome == "no_show":
        try:
            booking = get_booking(bid)
            if booking:
                from app import notify_waitlist    # lazy import: app.py imports back_office, not reverse
                threading.Thread(target=lambda: notify_waitlist(slug, dict(booking)), daemon=True).start()
        except Exception as e:
            print(f"[resolve_booking notify_waitlist] {e}")
    return {"ok": True, "outcome": outcome}


def confirm_tomorrow(slug):
    """Στέλνει μήνυμα επιβεβαίωσης σε ΟΛΑ τα αυριανά ραντεβού (μειώνει τα no-show).
    Dedup μέσω reminder_24 (μαρκάρεται ΜΟΝΟ όταν όντως φύγει μήνυμα — αλλιώς ξαναπροσπαθεί)."""
    biz = get_business(slug) or {}
    fn = biz.get("name") or "το ιατρείο"
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    with _conn() as c:
        rows = c.execute("""SELECT id, time, service, name, phone FROM bookings
                            WHERE slug=? AND date=? AND status='confirmed' AND reminder_24=0
                            ORDER BY time""", (slug, tomorrow)).fetchall()
    sent = queued = 0
    for b in rows:
        try:
            first = (b["name"] or "").split(" ")[0]
            text = (f"Γεια σας{(' ' + first) if first else ''}! Υπενθύμιση για το ραντεβού σας "
                    f"αύριο στις {b['time']}"
                    f"{(' (' + b['service'] + ')') if b['service'] else ''} στο {fn}. "
                    f"Αν δεν μπορείτε να έρθετε, ενημερώστε μας. Σας περιμένουμε!")
            if _send_patient_message(slug, b["phone"], b["name"], text, subject="Υπενθύμιση ραντεβού"):
                sent += 1
                # dedup flag ΜΟΝΟ αν είναι ακόμα confirmed (όχι ακυρωμένο/resolved στο μεταξύ)
                with _conn() as c:
                    c.execute("UPDATE bookings SET reminder_24=1 WHERE id=? AND slug=? AND status='confirmed'",
                              (b["id"], slug))
            else:
                log_comm(slug, b["phone"], "note", text, direction="out",
                         subject="Υπενθύμιση ραντεβού", status="queued")
                queued += 1
        except Exception as e:                 # ένα αποτυχημένο μήνυμα δεν ρίχνει όλο το batch
            print(f"[confirm_tomorrow] {e}")
            queued += 1
    return {"ok": True, "sent": sent, "queued": queued, "total": sent + queued, "date": tomorrow}


# ── Λίστα αναμονής (γέμισε ακυρωμένες/κενές θέσεις) ──────────────────────────
def add_waitlist(slug, name, phone, service="", note=""):
    phone = _clean_phone(phone)
    if not phone:
        return {"ok": False, "error": "Λείπει έγκυρο τηλέφωνο"}
    with _conn() as c:
        c.execute("""INSERT INTO bo_waitlist (slug, name, phone, service, note)
                     VALUES (?,?,?,?,?)""",
                  (slug, (name or "")[:120], phone, (service or "")[:120], (note or "")[:300]))
    return {"ok": True}


def list_waitlist(slug):
    with _conn() as c:
        rows = c.execute("""SELECT id, name, phone, service, note, status, created
                            FROM bo_waitlist WHERE slug=? AND status='waiting'
                            ORDER BY created""", (slug,)).fetchall()
    return [dict(r) for r in rows]


def remove_waitlist(slug, wid):
    with _conn() as c:
        c.execute("UPDATE bo_waitlist SET status='removed' WHERE slug=? AND id=?",
                  (slug, int(wid)))
    return {"ok": True}


def notify_waitlist(slug, wid):
    """«Άνοιξε θέση» μήνυμα σε έναν στη λίστα αναμονής (όταν ακυρώθηκε/no-show ραντεβού)."""
    biz = get_business(slug) or {}
    fn = biz.get("name") or "το ιατρείο"
    with _conn() as c:
        r = c.execute("SELECT name, phone, service FROM bo_waitlist WHERE slug=? AND id=?",
                      (slug, int(wid))).fetchone()
    if not r:
        return {"ok": False, "error": "Δεν βρέθηκε"}
    first = (r["name"] or "").split(" ")[0]
    text = (f"Γεια σας{(' ' + first) if first else ''}! Άνοιξε μια θέση στο {fn}"
            f"{(' για ' + r['service']) if r['service'] else ''}. "
            f"Θέλετε να σας την κρατήσουμε; Απαντήστε μας το συντομότερο.")
    if _send_patient_message(slug, r["phone"], r["name"], text, subject="Άνοιξε θέση"):
        with _conn() as c:
            c.execute("UPDATE bo_waitlist SET status='notified' WHERE slug=? AND id=?",
                      (slug, int(wid)))
        return {"ok": True, "msg": "Στάλθηκε ειδοποίηση — η θέση προσφέρθηκε."}
    log_comm(slug, r["phone"], "note", text, direction="out", subject="Άνοιξε θέση", status="queued")
    return {"ok": True, "msg": "Ετοιμάστηκε — θα φύγει μόλις συνδέσεις SMS ή email."}


# ════════════════════════════════════════════════════════════════════════════
# DASHBOARD SUMMARY — ένα call, όλο το back-office
# ════════════════════════════════════════════════════════════════════════════
def back_office_summary(slug):
    with _conn() as c:
        patients = c.execute("SELECT COUNT(*) FROM bo_patients WHERE slug=?", (slug,)).fetchone()[0]
        notes = c.execute("SELECT COUNT(*) FROM bo_patient_notes WHERE slug=?", (slug,)).fetchone()[0]
        invoices = c.execute("SELECT COUNT(*) FROM bo_invoices WHERE slug=?", (slug,)).fetchone()[0]
        suppliers = c.execute("SELECT COUNT(*) FROM bo_suppliers WHERE slug=?", (slug,)).fetchone()[0]
        inv_items = c.execute("SELECT COUNT(*) FROM bo_inventory WHERE slug=?", (slug,)).fetchone()[0]
        comms = c.execute("SELECT COUNT(*) FROM bo_comm_log WHERE slug=?", (slug,)).fetchone()[0]
        links = c.execute("SELECT COUNT(*) FROM bo_links WHERE slug=?", (slug,)).fetchone()[0]
        open_tasks = c.execute("SELECT COUNT(*) FROM bo_tasks WHERE slug=? AND status='open'", (slug,)).fetchone()[0]
    # HERO (το value που πληρώνεται): πόσα ανέκτησε ο AI (με ΟΝΟΜΑΤΑ) + πόσα € «στο τραπέζι τώρα»
    recovered = recovered_revenue(slug, 90)
    rec_events = recovered_events(slug, 90)
    rec_events.sort(key=lambda e: e.get("month", ""), reverse=True)
    return {
        "patients": patients, "notes": notes, "invoices": invoices,
        "suppliers": suppliers, "inventory_items": inv_items, "communications": comms,
        "graph_links": links, "open_tasks": open_tasks,
        "finance": finance_summary(slug),
        "calendar_today": calendar_utilization(slug),
        "recovered": recovered,                       # {recovered_eur, recovered_count, by_type}
        "recovered_events": rec_events[:6],            # named proof: [{name, service, value, type, month}]
        "money_on_table": money_on_table(slug),        # {at_risk_eur, outstanding_eur, dormant_*, top}
    }


def run_all_agents(slug):
    """Τρέχει όλη την back-office αλυσίδα μία φορά (sync → invoice → tasks)."""
    crm = sync_patients_from_bookings(slug)
    fin = autoinvoice_completed_bookings(slug)
    tasks = rebuild_tasks(slug)
    return {"crm": crm, "finance": fin, "tasks": tasks, "summary": back_office_summary(slug)}


def seed_demo_back_office(slug):
    """Γεμίζει realistic demo back-office (μία πηγή αλήθειας — καλείται από το seed route
    & το setup-demo-voice): προμηθευτές, αναλώσιμα (κάποια low-stock), έξοδα, μετά τρέχει
    όλους τους agents (φάκελοι+τιμολόγια+tasks), εξοφλεί ~2/3 τιμολογίων, σημειώσεις+comms.
    Idempotent-ish: αν υπάρχουν ήδη προμηθευτές, παραλείπει το seeding (ξανατρέχει μόνο agents)."""
    init_back_office()
    with _conn() as c:
        already = c.execute("SELECT COUNT(*) FROM bo_suppliers WHERE slug=?", (slug,)).fetchone()[0]
    if not already:
        s1 = add_supplier(slug, "MediShop Α.Ε.", "Ιατρικά αναλώσιμα", "Γ. Παπαδ.", "2310123456", "orders@medishop.gr")
        s2 = add_supplier(slug, "PhysioCare Hellas", "Εξοπλισμός", "Μ. Ιωάν.", "2310987654", "info@physiocare.gr")
        for name, qty, rl, unit, cost, cat, sid in [
            ("Γάντια νιτριλίου (κουτί)", 3, 5, "κουτί", 6.5, "Αναλώσιμα", s1),
            ("Ηλεκτρόδια TECAR", 1, 4, "σετ", 22.0, "Αναλώσιμα", s2),
            ("Αλοιφή υπερήχων (gel)", 8, 3, "τεμ", 4.2, "Αναλώσιμα", s1),
            ("Χαρτοβάμβακας", 2, 6, "ρολό", 1.8, "Αναλώσιμα", s1),
            ("Ελαστικοί επίδεσμοι", 12, 5, "τεμ", 2.5, "Αναλώσιμα", s2),
        ]:
            add_inventory_item(slug, name, qty=qty, reorder_level=rl, unit=unit,
                               unit_cost=cost, category=cat, supplier_id=sid)
        add_expense(slug, "Ενοίκιο", "Ιδιοκτήτης", 650, note="Μηνιαίο ενοίκιο χώρου")
        add_expense(slug, "Αναλώσιμα", "MediShop Α.Ε.", 180, vat=43.2, note="Παραγγελία γαντιών/gel")
        add_expense(slug, "ΔΕΗ", "ΔΕΗ", 95, note="Λογαριασμός ρεύματος")
    result = run_all_agents(slug)
    with _conn() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM bo_invoices WHERE slug=? ORDER BY id", (slug,)).fetchall()]
    for i, iid in enumerate(ids):
        if i % 3 != 0:
            with _conn() as c:
                tot = c.execute("SELECT total, paid FROM bo_invoices WHERE id=?", (iid,)).fetchone()
            if tot and (tot["paid"] or 0) < 0.01:
                record_payment(slug, iid, tot["total"], method="card" if i % 2 else "cash")
    with _conn() as c:
        pats = c.execute("SELECT phone, name FROM bo_patients WHERE slug=? LIMIT 6", (slug,)).fetchall()
    for p in pats:
        log_comm(slug, p["phone"], "sms", "Υπενθύμιση ραντεβού αύριο 17:00", subject="Reminder")
        add_patient_note(slug, p["phone"], "Αρχική αξιολόγηση: οσφυαλγία, πλάνο 8 συνεδρίες.",
                         kind="clinical", title="Πλάνο θεραπείας", author="Φυσικοθεραπευτής")
    rebuild_tasks(slug)
    return {"seeded": True, "result": result, "summary": back_office_summary(slug)}
