"""
provisioning.py — Instant self-serve setup ενός ολοκληρωμένου ιατρείου.

Ο γιατρός βάζει όνομα + ειδικότητα → σε ΔΕΥΤΕΡΟΛΕΠΤΑ στήνεται live, πλήρες σύστημα:
  1. Business (services/ωράριο/τύπος από vertical_templates ανά ειδικότητα)
  2. Demo δεδομένα (ραντεβού 4μήνου, no-shows, recovered, voice calls)
  3. Back-office (φάκελοι ασθενών, τιμολόγια, αποθήκη, προμηθευτές, tasks)
  4. Όλα διασυνδεδεμένα (Obsidian graph) + HMAC links (chat / dashboard / back-office)

Κανένα χειροκίνητο link, κανένα email στον Ντίνο — στήνεται μόνο του.
"""
import secrets
from datetime import datetime, timedelta

import database as db
from database import _conn, get_business, create_business
from vertical_templates import get_template, resolve_vertical
import back_office as bo
from voice.onboarding import slugify, unique_slug


# ── Demo data seeding (ίδια λογική με admin seeder, μικρότερο για ταχύτητα) ───
_DEMO_NAMES = [
    "Γιώργος Παπαδόπουλος", "Μαρία Νικολάου", "Δημήτρης Γεωργίου", "Ελένη Κωνσταντίνου",
    "Νίκος Αντωνίου", "Σοφία Δημητρίου", "Κώστας Βασιλείου", "Κατερίνα Ιωάννου",
    "Γιάννης Παππάς", "Άννα Μακρή", "Θανάσης Οικονόμου", "Δέσποινα Στεφάνου",
    "Παναγιώτης Λαμπρόπουλος", "Χριστίνα Αγγελή", "Στέλιος Καραγιάννης", "Βούλα Μητσοτάκη",
    "Μιχάλης Ροδίτης", "Ευαγγελία Σπανού", "Τάσος Βλαχάκης", "Ιωάννα Φωτίου",
    "Αντώνης Καλογερόπουλος", "Φωτεινή Δράκου", "Λευτέρης Μανωλάς", "Ρένα Ζαφειρίου",
    "Πέτρος Σαμαράς", "Αγγελική Τσιάρα", "Βαγγέλης Κουρής", "Μαρίνα Λέκκα",
    "Σπύρος Ηλιόπουλος", "Τζένη Βαρδάκη", "Χάρης Μπαλάσκας", "Όλγα Ρεντζή",
]
def _NM(idx):
    return _DEMO_NAMES[idx % len(_DEMO_NAMES)]


def _seed_bookings(slug, services, months=4):
    svcs = [s.get("name") for s in services if isinstance(s, dict)] or ["Συνεδρία"]
    durs = {s.get("name"): s.get("duration", 45) for s in services if isinstance(s, dict)}
    now = datetime.now()
    bk = ns = rec = vc = 0
    with _conn() as c:
        already = c.execute("SELECT COUNT(*) FROM bookings WHERE slug=? AND notes LIKE '%[DEMO]%'", (slug,)).fetchone()[0]
        if already:
            return {"status": "already_seeded", "bookings": already}
        for mo in range(months - 1, -1, -1):
            base = now - timedelta(days=mo * 30)
            n_book = 18 + (mo % 3) * 2
            for i in range(n_book):
                d = (base - timedelta(days=i)).strftime("%Y-%m-%d")
                # μελλοντικά λίγα confirmed, παλιά completed/no_show
                if mo == 0 and i < 4:
                    status, dd = "confirmed", (now + timedelta(days=i + 1)).strftime("%Y-%m-%d")
                else:
                    status, dd = ("no_show" if i < 3 else "completed"), d
                if status == "no_show":
                    ns += 1
                svc = svcs[i % len(svcs)]
                # OR IGNORE: το unique (slug,date,time) double-booking index (hardening 26/6)
                # πρέπει να προσπερνά demo συγκρούσεις — το provisioning δεν επιτρέπεται να 500άρει.
                cur = c.execute("INSERT OR IGNORE INTO bookings (slug,date,time,service,duration,name,phone,email,status,notes,reminder_24,reminder_4) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,1,1)",
                          (slug, dd, f"{9 + (i % 9)}:00", svc, durs.get(svc, 45),
                           _NM(mo * 23 + i), "690" + str(1000000 + mo * 100 + i),
                           f"demo{mo}{i}@example.gr", status, "[DEMO]"))
                bk += cur.rowcount
            for j in range(10):
                ts = (base - timedelta(days=j, hours=2)).strftime("%Y-%m-%d %H:%M:%S")
                c.execute("INSERT INTO voice_calls (business_slug,from_number,started_at,duration_seconds,cost_usd,summary) "
                          "VALUES (?,?,?,?,?,?)", (slug, "690" + str(2000000 + mo * 100 + j), ts, 92, 0.11, "[DEMO] Έκλεισε ραντεβού"))
                vc += 1
            n_rec = 5 if mo == 0 else 3
            for k in range(n_rec):
                ts = (base - timedelta(days=k)).strftime("%Y-%m-%d %H:%M:%S")
                c.execute("INSERT INTO outbound_jobs (slug,job_type,channel,customer_phone,status,scheduled_at,created,result) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (slug, "no_show_recovery", "sms", "690" + str(3000000 + mo * 100 + k), "done", ts, ts, "[DEMO] rebooked"))
                rec += 1
        c.commit()
    # Recovered cohort: ασθενείς που έκαναν no-show → recovery message → ΞΑΝΑΕΚΛΕΙΣΑΝ.
    # Δίνει ρεαλιστικό 'Recovered-Revenue Ledger' στο demo (πραγματικό attribution, όχι fake νούμερο).
    svc0 = svcs[0]
    rebooked = 0
    with _conn() as c:
        for k in range(6):
            ph = "6955" + str(100000 + k)
            rnm = _NM(k * 7 + 11)   # ίδιο όνομα σε job + booking ώστε το attribution να δείχνει ΟΝΟΜΑ
            job_ts = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute("INSERT INTO outbound_jobs (slug,job_type,channel,customer_phone,customer_name,status,scheduled_at,created,result) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (slug, ("win_back" if k % 2 else "no_show_recovery"), "email", ph,
                       rnm, "done", job_ts, job_ts, "[DEMO] recovered"))
            fut = (now + timedelta(days=k + 2)).strftime("%Y-%m-%d")
            # 13:30: ώρα που το κύριο loop δεν κλείνει ποτέ (πάντα :00) → κανένα conflict με το
            # unique double-booking index· OR IGNORE ως δίχτυ για μελλοντικό drift.
            cur = c.execute("INSERT OR IGNORE INTO bookings (slug,date,time,service,duration,name,phone,email,status,notes,reminder_24,reminder_4) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,1,1)",
                      (slug, fut, "13:30", svc0, durs.get(svc0, 45),
                       rnm, ph, "", "confirmed", "[DEMO][RECOVERED]"))
            rebooked += cur.rowcount
        c.commit()

    # ── ΣΗΜΕΡΑ + ΑΥΡΙΟ: ζωντανή «Ημέρα» οθόνη (αλλιώς το κύριο tab ανοίγει άδειο) ──
    today_str = now.strftime("%Y-%m-%d")
    tomo_str  = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    # ρεαλιστική μέρα: 3 ολοκληρωμένα (→ ταμείο), 1 no-show, 2 επερχόμενα
    today_plan = [("09:00", "completed"), ("10:00", "completed"), ("11:00", "no_show"),
                  ("12:00", "completed"), ("16:00", "confirmed"), ("17:00", "confirmed")]
    with _conn() as c:
        for n, (tm, st) in enumerate(today_plan):
            svc = svcs[n % len(svcs)]
            c.execute("INSERT OR IGNORE INTO bookings (slug,date,time,service,duration,name,phone,email,status,notes,reminder_24,reminder_4) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,1,1)",
                      (slug, today_str, tm, svc, durs.get(svc, 45), _NM(700 + n),
                       "697" + str(7000000 + n), "", st, "[DEMO]"))
        # αυριανά ΑΝΕΠΙΒΕΒΑΙΩΤΑ (reminder_24=0) → το κουμπί «Επιβεβαίωσε αυριανά» έχει δουλειά
        for n, tm in enumerate(["09:30", "11:30", "18:00"]):
            svc = svcs[n % len(svcs)]
            c.execute("INSERT OR IGNORE INTO bookings (slug,date,time,service,duration,name,phone,email,status,notes,reminder_24,reminder_4) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
                      (slug, tomo_str, tm, svc, durs.get(svc, 45), _NM(720 + n),
                       "697" + str(7200000 + n), "", "confirmed", "[DEMO]"))
        c.commit()
    # λίστα αναμονής demo (bo.add_waitlist διαχειρίζεται δικό του connection)
    for n, note in enumerate(["προτιμά πρωί", "μετά τις 17:00"]):
        bo.add_waitlist(slug, _NM(740 + n), "697" + str(7400000 + n),
                        svcs[n % len(svcs)], note)

    return {"status": "seeded", "bookings": bk + len(today_plan) + 3,
            "no_shows": ns + 1, "recovered": rec, "voice_calls": vc,
            "rebooked": rebooked, "today": len(today_plan)}


def _seed_back_office(slug, vertical_key):
    """Προμηθευτές/αναλώσιμα ανά ειδικότητα + έξοδα, μετά τρέχει όλους τους agents."""
    bo.init_back_office()
    # vertical-aware αναλώσιμα
    INV = {
        "physio": [("Αλοιφή υπερήχων (gel)", 8, 3, "τεμ", 4.2), ("Ηλεκτρόδια TECAR", 1, 4, "σετ", 22.0),
                   ("Ελαστικοί επίδεσμοι", 12, 5, "τεμ", 2.5), ("Χαρτοβάμβακας", 2, 6, "ρολό", 1.8)],
        "dental": [("Αναισθητικό (φύσιγγες)", 4, 10, "τεμ", 0.8), ("Γάντια νιτριλίου", 3, 5, "κουτί", 6.5),
                   ("Σύνθετη ρητίνη", 2, 4, "τεμ", 18.0), ("Βελόνες αναισθησίας", 6, 8, "κουτί", 9.0)],
        "dermatology": [("Κρυοθεραπεία (υγρό άζωτο)", 1, 2, "φιάλη", 35.0), ("Γάντια", 4, 5, "κουτί", 6.5),
                        ("Αντισηπτικό", 3, 4, "τεμ", 3.2), ("Νυστέρια βιοψίας", 5, 8, "τεμ", 1.5)],
        "medical": [("Γάντια εξεταστικά", 3, 5, "κουτί", 6.5), ("Σύριγγες", 20, 30, "τεμ", 0.1),
                    ("Γλωσσοπίεστρα", 10, 20, "τεμ", 0.05), ("Αντισηπτικό χεριών", 2, 4, "τεμ", 3.5)],
    }
    items = INV.get(vertical_key, INV["medical"])
    s1 = bo.add_supplier(slug, "MediShop Α.Ε.", "Ιατρικά αναλώσιμα", "Γ. Παπαδ.", "2310123456", "orders@medishop.gr")
    for name, qty, rl, unit, cost in items:
        bo.add_inventory_item(slug, name, qty=qty, reorder_level=rl, unit=unit, unit_cost=cost,
                              category="Αναλώσιμα", supplier_id=s1)
    bo.add_expense(slug, "Ενοίκιο", "Ιδιοκτήτης", 650, note="Μηνιαίο ενοίκιο")
    bo.add_expense(slug, "Αναλώσιμα", "MediShop Α.Ε.", 180, vat=43.2, note="Παραγγελία αναλωσίμων")
    result = bo.run_all_agents(slug)
    # ~2/3 τιμολόγια πληρωμένα → ρεαλιστικά υπόλοιπα
    with _conn() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM bo_invoices WHERE slug=? ORDER BY id", (slug,)).fetchall()]
    for i, iid in enumerate(ids):
        if i % 3 != 0:
            with _conn() as c:
                tot = c.execute("SELECT total FROM bo_invoices WHERE id=?", (iid,)).fetchone()
            if tot:
                bo.record_payment(slug, iid, tot[0], method="card" if i % 2 else "cash")
    # backdate demo πληρωμές στην ημερομηνία του ραντεβού — αλλιώς ΟΛΕΣ μετρούν paid «σήμερα»
    # → φουσκωμένο «ταμείο σήμερα» (collected >> expected = φαίνεται σπασμένο)
    with _conn() as c:
        c.execute("""UPDATE bo_payments SET paid_at = COALESCE((
                        SELECT b.date || ' 12:00:00' FROM bo_invoices i JOIN bookings b ON b.id=i.booking_id
                        WHERE i.id = bo_payments.invoice_id), paid_at)
                     WHERE slug=?""", (slug,))
        c.commit()
    # demo επικοινωνίες + σημειώσεις φακέλου
    note_by_vert = {
        "physio": ("Πλάνο θεραπείας", "Αρχική αξιολόγηση: οσφυαλγία, πλάνο 8 συνεδρίες."),
        "dental": ("Οδοντικό ιστορικό", "Έλεγχος ρουτίνας: 1 σφράγισμα #36, recall 6 μήνες."),
        "dermatology": ("Δερματολογικό", "Σπίλος υπό παρακολούθηση, φωτο follow-up 6 μήνες."),
        "medical": ("Ιατρικό σημείωμα", "Check-up ρουτίνας, εργαστηριακά εντός ορίων."),
    }
    title, body = note_by_vert.get(vertical_key, note_by_vert["medical"])
    with _conn() as c:
        pats = c.execute("SELECT phone, name FROM bo_patients WHERE slug=? LIMIT 6", (slug,)).fetchall()
    for p in pats:
        bo.log_comm(slug, p["phone"], "sms", "Υπενθύμιση ραντεβού αύριο", subject="Reminder")
        bo.add_patient_note(slug, p["phone"], body, kind="clinical", title=title, author="Ιατρός")
    bo.rebuild_tasks(slug)
    return result


def _links(slug):
    try:
        from routes.dashboard_routes import dash_token
        t = dash_token(slug)
    except Exception:
        t = ""
    base = "https://www.ran-tevu.gr"
    return {
        "setup":       f"{base}/{slug}/setup?t={t}",
        "chat":        f"{base}/{slug}",
        "dashboard":   f"{base}/{slug}/dashboard?t={t}",
        "back_office": f"{base}/{slug}/back-office?t={t}",
        "token": t,
    }


def reset_business_data(slug):
    """Μηδενίζει ΟΛΑ τα λειτουργικά δεδομένα ενός slug (ραντεβού, ασθενείς, τιμολόγια,
    κλήσεις, jobs, tasks) ΚΡΑΤΩΝΤΑΣ το business config (όνομα/υπηρεσίες/ώρες).
    Χρήση: καθάρισμα demo ώστε να δείχνει 0 — ΟΧΙ fake νούμερα. ΠΡΟΣΟΧΗ: σε πραγματικό
    πελάτη σβήνει τα δεδομένα του· κάλεσέ το ΜΟΝΟ σκόπιμα (demo reset)."""
    tables = [
        ("bookings", "slug"), ("outbound_jobs", "slug"), ("voice_calls", "business_slug"),
        ("bo_invoices", "slug"), ("bo_payments", "slug"), ("bo_patients", "slug"),
        ("bo_patient_notes", "slug"), ("bo_inventory", "slug"), ("bo_suppliers", "slug"),
        ("bo_expenses", "slug"), ("bo_comm_log", "slug"), ("bo_tasks", "slug"),
        ("bo_documents", "slug"), ("bo_links", "slug"), ("bo_supplier_orders", "slug"),
        ("client_profiles", "slug"),
    ]
    wiped = {}
    with _conn() as c:
        for tbl, col in tables:
            try:
                cur = c.execute(f"DELETE FROM {tbl} WHERE {col}=?", (slug,))
                if cur.rowcount:
                    wiped[tbl] = cur.rowcount
            except Exception:
                pass  # ο πίνακας μπορεί να μην υπάρχει
        c.commit()
    return {"slug": slug, "wiped": wiped}


def load_sample_data(slug):
    """Φορτώνει ΡΕΑΛΙΣΤΙΚΟ δείγμα (ασθενείς/ραντεβού/recovery/τιμολόγια) σε ΥΠΑΡΧΟΝ slug,
    ώστε ο ιδιοκτήτης να ΔΕΙ πώς δουλεύει το back office + ο AI βοηθός πάνω σε δεδομένα.
    Ελεγχόμενο από τον χρήστη (κουμπί) — ΟΧΙ μόνιμο. Καθαρίζεται με reset_business_data()."""
    import json as _json
    biz = get_business(slug)
    if not biz:
        return {"error": "not found"}
    vkey = resolve_vertical(biz.get("type") or "medical")
    tmpl = get_template(vkey)
    services = biz.get("services")
    if isinstance(services, str):
        try:
            services = _json.loads(services)
        except Exception:
            services = None
    services = services or tmpl["services"]
    bookings = _seed_bookings(slug, services)
    _seed_back_office(slug, vkey)
    return {"ok": True, "bookings": bookings}


def provision(name, vertical_key, owner_name="", email="", phone="", address="",
              demo=False, hours=None, slug=None):   # default ΚΑΘΑΡΟ — 0 fake νούμερα (demo=True μόνο αν ζητηθεί ρητά)
    """Στήνει πλήρες ιατρείο. Επιστρέφει {slug, links, summary}. Idempotent αν δοθεί υπάρχον slug."""
    # Self-sufficient: εξασφάλισε ότι υπάρχουν τα back-office tables (bo_patients/invoices/tasks)
    # πριν το back_office_summary — ώστε ένας πληρωμένος γιατρός να ΜΗΝ πέφτει ποτέ σε crash αν
    # το startup δεν έτρεξε το init (idempotent: CREATE TABLE IF NOT EXISTS).
    try:
        bo.init_back_office()
    except Exception as _e:
        print(f"[PROVISION] back-office init skipped: {_e}")
    vkey = resolve_vertical(vertical_key)
    tmpl = get_template(vkey)
    slug = slug or unique_slug(slugify(name) or f"{vkey}-{secrets.token_hex(2)}")
    if get_business(slug):
        # Idempotent — υπάρχει ήδη, μην ξαναστήσεις
        return {"slug": slug, "vertical": vkey, "label": tmpl["label"],
                "links": _links(slug), "summary": bo.back_office_summary(slug),
                "seed": {"status": "already_exists"}}
    cfg = {
        "name": name or tmpl["label"],
        "type": tmpl["type"],
        "tagline": tmpl["label"],
        "phone": phone or "2100000000",
        "address": address or "Ελλάδα",
        "owner_email": email or "demo@ran-tevu.gr",
        "owner_name": owner_name or "Owner",
        "hours": hours or {"Δευτέρα": "09:00 - 21:00", "Τρίτη": "09:00 - 21:00",
                           "Τετάρτη": "09:00 - 21:00", "Πέμπτη": "09:00 - 21:00",
                           "Παρασκευή": "09:00 - 21:00", "Σάββατο": "09:00 - 14:00",
                           "Κυριακή": "Κλειστά"},
        "services": tmpl["services"],
        "slot_minutes": 15, "day_start": "09:00", "day_end": "21:00",
        "admin_token": secrets.token_hex(8),
    }
    create_business(slug, cfg)
    seed_info = {}
    if demo:
        seed_info["bookings"] = _seed_bookings(slug, tmpl["services"])
        seed_info["back_office"] = _seed_back_office(slug, vkey)
    return {
        "slug": slug, "vertical": vkey, "label": tmpl["label"],
        "links": _links(slug),
        "summary": bo.back_office_summary(slug),
        "seed": seed_info,
    }
