"""
back_office_assistant.py — AI Γραμματέας (tool-calling, human-in-the-loop)

Ο γιατρός γράφει σε φυσική γλώσσα ("κλείσε ραντεβού στον Γιώργο αύριο 5",
"ακύρωσε της Μαρίας", "έκδωσε τιμολόγιο €50 στον Νίκο"). Το Claude καταλαβαίνει
και ΣΧΕΔΙΑΖΕΙ τις ενέργειες.

  • READ tools (αναζητήσεις) τρέχουν ΑΥΤΟΜΑΤΑ ώστε το AI να σχεδιάσει σωστά
    (π.χ. να βρει το booking_id πριν προτείνει ακύρωση).
  • WRITE tools (αλλαγές δεδομένων) ΔΕΝ εκτελούνται στο /ask — επιστρέφονται ως
    "proposed actions" που χρειάζονται την Έγκριση του γιατρού. Μετά την έγκριση,
    το execute_action() τα εκτελεί πραγματικά.

Stateless: τα proposed actions επιστρέφονται στο frontend και ξαναέρχονται στο
/execute όταν ο γιατρός πατήσει Έγκριση. Κανένα state δεν κρατιέται server-side.
"""
import os
import json
from datetime import datetime, timedelta

import database as db
import back_office as bo

MODEL = "claude-sonnet-4-6"   # φθηνό+γρήγορο· το NLU→action mapping δεν θέλει Opus

# ════════════════════════════════════════════════════════════════════════════
# TOOL SCHEMAS
# ════════════════════════════════════════════════════════════════════════════
READ_TOOLS = {"find_patient", "check_availability", "list_unpaid_invoices",
              "schedule_for_date", "overview", "list_debtors", "low_stock",
              "open_tasks", "patient_history", "recovered_revenue", "query_data"}
WRITE_TOOLS = {"book_appointment", "cancel_appointment", "reschedule_appointment",
               "create_invoice", "record_payment", "send_message",
               "add_patient", "add_note", "winback"}

TOOLS = [
    {"name": "find_patient",
     "description": "Βρες ασθενή με όνομα ή τηλέφωνο. Επιστρέφει τα στοιχεία του + τα ραντεβού του "
                    "(με booking_id) ώστε να μπορείς μετά να ακυρώσεις/μεταθέσεις. ΧΡΗΣΙΜΟΠΟΙΗΣΕ ΤΟ ΠΑΝΤΑ "
                    "πριν προτείνεις ακύρωση/μετάθεση/τιμολόγιο για υπάρχοντα ασθενή.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "όνομα ή τηλέφωνο ασθενή"}}, "required": ["query"]}},
    {"name": "check_availability",
     "description": "Δες ποιες ώρες είναι ελεύθερες μια συγκεκριμένη μέρα, πριν κλείσεις ραντεβού.",
     "input_schema": {"type": "object", "properties": {
         "date": {"type": "string", "description": "ημερομηνία YYYY-MM-DD"}}, "required": ["date"]}},
    {"name": "list_unpaid_invoices",
     "description": "Λίστα ανεξόφλητων τιμολογίων (με invoice_id) — προαιρετικά φιλτραρισμένα ανά ασθενή.",
     "input_schema": {"type": "object", "properties": {
         "patient_query": {"type": "string", "description": "όνομα/τηλέφωνο ασθενή (προαιρετικό)"}}}},
    {"name": "schedule_for_date",
     "description": "Τα ραντεβού μιας μέρας (π.χ. «τι έχω σήμερα/αύριο/Παρασκευή»). Χωρίς date = σήμερα.",
     "input_schema": {"type": "object", "properties": {
         "date": {"type": "string", "description": "YYYY-MM-DD (προαιρετικό, default σήμερα)"}}}},
    {"name": "overview",
     "description": "Γενική εικόνα ιατρείου: έσοδα/χρωστούμενα/κέρδος 30 ημερών, # ασθενών, εκκρεμείς εργασίες, πληρότητα σήμερα. Για «πώς πάμε;», «σύνοψη».",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_debtors",
     "description": "Ποιοι ασθενείς χρωστούν (ανά ασθενή, με ποσό). Για «ποιος μου χρωστάει».",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "low_stock",
     "description": "Αναλώσιμα που τελειώνουν & χρειάζονται παραγγελία. Για «τι να παραγγείλω».",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "open_tasks",
     "description": "Οι εργασίες/προτεραιότητες της ημέρας (recall, χρεωστικά, παραγγελίες). Για «τι να κάνω σήμερα».",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "patient_history",
     "description": "Πλήρης φάκελος ασθενή: ραντεβού, τιμολόγια, σημειώσεις, υπόλοιπο. Για «δείξε μου τον/την Χ».",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "όνομα ή τηλέφωνο ασθενή"}}, "required": ["query"]}},
    {"name": "recovered_revenue",
     "description": "Πόσα € ανέκτησε ο Ran-Tevu (ασθενείς που ξαναέκλεισαν μετά από no-show recovery/win-back). Για «πόσα μου έφερες πίσω;», «πόσα ανέκτησα;».",
     "input_schema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "περίοδος σε ημέρες (default 90)"}}}},
    {"name": "query_data",
     "description": (
         "ΓΕΝΙΚΗ ΙΣΧΥΣ: τρέξε ΟΠΟΙΟΔΗΠΟΤΕ read-only SQL SELECT στα δεδομένα του ιατρείου για να "
         "απαντήσεις ερωτήσεις/αναλύσεις/λίστες που ΔΕΝ καλύπτουν τα ειδικά tools "
         "(π.χ. «πόσοι ασθενείς ήρθαν Τρίτες», «ασθενείς χωρίς email», «μέση αξία ραντεβού ανά μήνα», "
         "«top 10 ασθενείς ανά έσοδα», λίστες για export). ΥΠΟΧΡΕΩΤΙΚΟ: κάθε query ΠΡΕΠΕΙ να έχει φίλτρο "
         "slug = '{SLUG}'. Μόνο ΑΝΑΓΝΩΣΗ — για αλλαγές χρησιμοποίησε τα write tools.\n"
         "ΠΙΝΑΚΕΣ (key columns): bookings(slug,date,time,service,duration,name,phone,email,status,created) · "
         "bo_patients(slug,phone,name,email,status,created) · "
         "bo_invoices(slug,number,patient_phone,patient_name,issue_date,service,total,paid,status) · "
         "bo_payments(slug,invoice_id,amount,method,paid_at) · "
         "bo_tasks(slug,title,kind,priority,status) · "
         "bo_inventory(slug,name,qty,reorder_level,unit) · bo_expenses(slug,category,vendor,amount,expense_date) · "
         "bo_comm_log(slug,patient_phone,channel,direction,body,created) · "
         "outbound_jobs(slug,job_type,channel,customer_phone,status,created) · "
         "voice_calls(business_slug,started_at,duration_seconds,summary). "
         "(Ευαίσθητα πεδία π.χ. σημειώσεις είναι κρυπτογραφημένα.)"),
     "input_schema": {"type": "object", "properties": {
         "sql": {"type": "string", "description": "ένα SELECT statement, με slug = '{SLUG}' φίλτρο"},
         "purpose": {"type": "string", "description": "τι ψάχνεις (σύντομα, για λογ.)"}},
         "required": ["sql"]}},

    {"name": "book_appointment",
     "description": "Κλείσε νέο ραντεβού. Αν λείπει η υπηρεσία, χρησιμοποίησε την προεπιλεγμένη.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "phone": {"type": "string"},
         "date": {"type": "string", "description": "YYYY-MM-DD"},
         "time": {"type": "string", "description": "HH:MM"},
         "service": {"type": "string"}},
         "required": ["name", "phone", "date", "time"]}},
    {"name": "cancel_appointment",
     "description": "Ακύρωσε υπάρχον ραντεβού (χρειάζεσαι booking_id από το find_patient).",
     "input_schema": {"type": "object", "properties": {
         "booking_id": {"type": "integer"}, "patient_name": {"type": "string"}},
         "required": ["booking_id"]}},
    {"name": "reschedule_appointment",
     "description": "Μετάθεσε υπάρχον ραντεβού σε νέα μέρα/ώρα (booking_id + phone από find_patient).",
     "input_schema": {"type": "object", "properties": {
         "booking_id": {"type": "integer"}, "phone": {"type": "string"},
         "new_date": {"type": "string"}, "new_time": {"type": "string"},
         "patient_name": {"type": "string"}},
         "required": ["booking_id", "phone", "new_date", "new_time"]}},
    {"name": "create_invoice",
     "description": "Έκδωσε τιμολόγιο για ασθενή. Το ποσό είναι μικτό (με ΦΠΑ αν ισχύει).",
     "input_schema": {"type": "object", "properties": {
         "phone": {"type": "string"}, "name": {"type": "string"},
         "service": {"type": "string"}, "amount": {"type": "number"}},
         "required": ["phone", "name", "service", "amount"]}},
    {"name": "record_payment",
     "description": "Κατέγραψε πληρωμή για τιμολόγιο (invoice_id από list_unpaid_invoices). "
                    "Χωρίς ποσό = εξόφληση όλου του υπολοίπου.",
     "input_schema": {"type": "object", "properties": {
         "invoice_id": {"type": "integer"}, "amount": {"type": "number"},
         "patient_name": {"type": "string"}},
         "required": ["invoice_id"]}},
    {"name": "send_message",
     "description": "Στείλε μήνυμα σε ασθενή (υπενθύμιση/ενημέρωση). Καταγράφεται στο ιστορικό επικοινωνίας. "
                    "Αν ο ασθενής έχει email στέλνεται email· αλλιώς SMS (αν είναι ρυθμισμένο).",
     "input_schema": {"type": "object", "properties": {
         "phone": {"type": "string"}, "name": {"type": "string"},
         "text": {"type": "string"}, "channel": {"type": "string", "enum": ["sms", "email"]}},
         "required": ["phone", "text"]}},
    {"name": "add_patient",
     "description": "Πρόσθεσε νέο φάκελο ασθενή.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"}},
         "required": ["name", "phone"]}},
    {"name": "add_note",
     "description": "Πρόσθεσε κλινική σημείωση στον φάκελο ασθενή.",
     "input_schema": {"type": "object", "properties": {
         "phone": {"type": "string"}, "name": {"type": "string"},
         "title": {"type": "string"}, "body": {"type": "string"}},
         "required": ["phone", "body"]}},
    {"name": "winback",
     "description": "Προγραμμάτισε αυτόματες επανακλήσεις/μηνύματα σε ασθενείς που δεν έχουν έρθει εδώ και καιρό.",
     "input_schema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "ημέρες αδράνειας (default 90)"},
         "limit": {"type": "integer", "description": "μέγιστος αριθμός (default 20)"}}}},
]


# ════════════════════════════════════════════════════════════════════════════
# READ TOOL EXECUTORS (τρέχουν αυτόματα στο /ask)
# ════════════════════════════════════════════════════════════════════════════
def _read_find_patient(slug, query):
    q = (query or "").strip()
    like = f"%{q}%"
    with db._conn() as c:
        rows = c.execute(
            """SELECT phone, name, email FROM bo_patients
               WHERE slug=? AND (name LIKE ? OR phone LIKE ?) LIMIT 8""",
            (slug, like, like)).fetchall()
        # fallback: αναζήτηση και στα bookings (ασθενής που δεν έχει ακόμα φάκελο)
        if not rows:
            rows = c.execute(
                """SELECT DISTINCT phone, name, email FROM bookings
                   WHERE slug=? AND (name LIKE ? OR phone LIKE ?) LIMIT 8""",
                (slug, like, like)).fetchall()
    out = []
    with db._conn() as c:
        for r in rows:
            appts = c.execute(
                """SELECT id AS booking_id, date, time, service, status FROM bookings
                   WHERE slug=? AND phone=? AND status!='cancelled'
                   ORDER BY date DESC, time DESC LIMIT 10""",
                (slug, r["phone"])).fetchall()
            out.append({"name": r["name"], "phone": r["phone"], "email": r["email"],
                        "appointments": [dict(a) for a in appts]})
    return {"matches": out, "count": len(out)}


def _read_check_availability(slug, date_str):
    biz = db.get_business(slug)
    if not biz:
        return {"error": "Δεν βρέθηκε η επιχείρηση"}
    day_start, day_end, is_closed = db.get_day_hours(biz, date_str)
    if is_closed:
        return {"date": date_str, "closed": True, "slots": []}
    slots = db.get_available_slots(
        slug, date_str, int(biz.get("slot_minutes") or 30),
        day_start, day_end, int(biz.get("slot_minutes") or 30),
        biz.get("closed_days"), biz.get("blocked_dates"))
    return {"date": date_str, "closed": False, "free_slots": slots[:20]}


def _read_list_unpaid(slug, patient_query=None):
    with db._conn() as c:
        if patient_query:
            like = f"%{patient_query.strip()}%"
            rows = c.execute(
                """SELECT id AS invoice_id, number, patient_name, patient_phone,
                          service, total, paid, (total-paid) AS remaining
                   FROM bo_invoices WHERE slug=? AND status!='paid'
                   AND (patient_name LIKE ? OR patient_phone LIKE ?)
                   ORDER BY issue_date DESC LIMIT 30""",
                (slug, like, like)).fetchall()
        else:
            rows = c.execute(
                """SELECT id AS invoice_id, number, patient_name, patient_phone,
                          service, total, paid, (total-paid) AS remaining
                   FROM bo_invoices WHERE slug=? AND status!='paid'
                   ORDER BY issue_date DESC LIMIT 30""",
                (slug,)).fetchall()
    return {"unpaid_invoices": [dict(r) for r in rows]}


def _read_schedule(slug, date_str=None):
    day = (date_str or "").strip() or datetime.now().strftime("%Y-%m-%d")
    rows = db.get_bookings_for_date(slug, day)
    appts = [{"time": r.get("time"), "name": r.get("name"), "service": r.get("service"),
              "phone": r.get("phone"), "status": r.get("status"), "source": "Ran-Tevu"} for r in rows]
    # CONTEXT: συγχώνευσε το ΠΡΑΓΜΑΤΙΚΟ Google Calendar του γιατρού (αν συνδεδεμένο)
    try:
        biz = db.get_business(slug) or {}
        cal_id = (biz.get("google_calendar_id") or "").strip()
        if cal_id:
            from calendar_service import list_events
            for e in list_events(cal_id, day):
                appts.append({"time": e.get("time", ""), "name": e.get("title", ""),
                              "service": "(από το ημερολόγιό σου)", "status": "ημερολόγιο",
                              "source": "Google Calendar"})
    except Exception as _e:
        print(f"[assistant schedule] calendar merge skipped: {_e}")
    appts.sort(key=lambda a: a.get("time") or "")
    return {"date": day, "count": len(appts), "appointments": appts,
            "calendar_connected": bool((db.get_business(slug) or {}).get("google_calendar_id"))}


def _read_patient_history(slug, query):
    fp = _read_find_patient(slug, query)
    matches = fp.get("matches", [])
    if not matches:
        return {"found": False, "msg": "Δεν βρέθηκε ασθενής."}
    phone = matches[0]["phone"]
    full = bo.patient_360(slug, phone)
    return {"found": True, "profile": full.get("profile"), "balance": full.get("balance"),
            "appointments": full.get("appointments", [])[:10],
            "invoices": full.get("invoices", [])[:10], "notes": full.get("notes", [])[:10]}


# Πίνακες που επιτρέπεται να διαβάσει ο Γραμματέας (ΟΛΟΙ έχουν στήλη slug).
# Ευαίσθητοι ΕΚΤΟΣ: businesses(admin_token/smtp), subscriptions, voice_calls(transcripts),
# customer_memory, audit/consent_logs, inbox_*, outreach_*, voice_agents — δεν διαβάζονται.
_QD_ALLOW = {
    "bookings", "bo_patients", "bo_invoices", "bo_payments", "bo_tasks",
    "bo_inventory", "bo_suppliers", "bo_expenses", "bo_comm_log",
    "bo_patient_notes", "bo_documents", "client_profiles", "outbound_jobs",
}


def _read_query_data(slug, sql):
    """ΓΕΝΙΚΗ read-only ισχύς, ΑΣΦΑΛΗΣ multi-tenant:
    - strip σχόλια, μόνο ΕΝΑ SELECT/WITH, no mutations
    - ALLOWLIST πινάκων μέσω full-schema scan (ό,τι ΔΕΝ είναι στη λίστα → reject· πιάνει
      comma-joins/subqueries/CTE references σε ευαίσθητους πίνακες)
    - SCOPING με temp views προ-φιλτραρισμένα στο slug + rewrite table→view: η ΠΗΓΗ
      δεδομένων είναι ήδη φιλτραρισμένη, άρα tautology/comment ΔΕΝ ξεφεύγουν (no cross-tenant)."""
    import re
    if not re.match(r"^[A-Za-z0-9_-]+$", slug or ""):   # alphanumeric+_-: ασφαλές να γίνει inline στο view
        return {"error": "invalid slug"}
    q = (sql or "").strip()
    # strip σχόλια (κλείνει το comment-bypass)
    q = re.sub(r"--[^\n]*", " ", q)
    q = re.sub(r"/\*.*?\*/", " ", q, flags=re.S)
    q = q.strip().rstrip(";").strip()
    if not q:
        return {"error": "Άδειο query."}
    low = q.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return {"error": "Μόνο SELECT επιτρέπεται (ανάγνωση)."}
    if ";" in q:
        return {"error": "Μόνο ένα statement επιτρέπεται."}
    if re.search(r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex|truncate)\b", low):
        return {"error": "Μη επιτρεπτή λειτουργία — μόνο ανάγνωση."}
    try:
        with db._conn() as c:
            all_tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
            referenced = {t for t in all_tables if re.search(rf"\b{re.escape(t)}\b", low)}
            disallowed = referenced - _QD_ALLOW
            if disallowed:
                return {"error": f"Επιτρέπονται μόνο: {', '.join(sorted(_QD_ALLOW))}. "
                                 f"Μη επιτρεπτό: {', '.join(sorted(disallowed))}"}
            used = referenced & _QD_ALLOW
            if not used:
                return {"error": "Πρόσθεσε FROM σε επιτρεπτό πίνακα (π.χ. bookings, bo_patients, bo_invoices)."}
            # SCOPE: temp views κλειδωμένα στο ιατρείο + rewrite table→view (case-insensitive)
            rewritten = q
            for t in used:
                c.execute(f"DROP VIEW IF EXISTS _s_{t}")
                c.execute(f"CREATE TEMP VIEW _s_{t} AS SELECT * FROM {t} WHERE slug = '{slug}'")
                rewritten = re.sub(rf"\b{t}\b", f"_s_{t}", rewritten, flags=re.I)
            try:
                cur = c.execute(rewritten)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(200)
                out = {"columns": cols, "rows": [dict(r) for r in rows],
                       "count": len(rows), "truncated": len(rows) == 200}
            finally:
                for t in used:
                    try:
                        c.execute(f"DROP VIEW IF EXISTS _s_{t}")
                    except Exception:
                        pass
        return out
    except Exception as e:
        return {"error": f"Σφάλμα query: {str(e)[:200]}"}


def _run_read_tool(slug, name, inp):
    try:
        if name == "find_patient":
            return _read_find_patient(slug, inp.get("query", ""))
        if name == "check_availability":
            return _read_check_availability(slug, inp.get("date", ""))
        if name == "list_unpaid_invoices":
            return _read_list_unpaid(slug, inp.get("patient_query"))
        if name == "schedule_for_date":
            return _read_schedule(slug, inp.get("date"))
        if name == "overview":
            return bo.back_office_summary(slug)
        if name == "list_debtors":
            return {"debtors": bo.outstanding_balances(slug)}
        if name == "low_stock":
            return {"low_stock": bo.low_stock(slug)}
        if name == "open_tasks":
            with db._conn() as c:
                rows = c.execute(
                    """SELECT title, kind, priority FROM bo_tasks WHERE slug=? AND status='open'
                       ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id
                       LIMIT 50""", (slug,)).fetchall()
            return {"tasks": [dict(r) for r in rows]}
        if name == "patient_history":
            return _read_patient_history(slug, inp.get("query", ""))
        if name == "recovered_revenue":
            return db.recovered_revenue(slug, days=int(inp.get("days") or 90))
        if name == "query_data":
            return _read_query_data(slug, inp.get("sql", ""))
    except Exception as e:
        return {"error": str(e)[:200]}
    return {"error": "unknown_read_tool"}


# ════════════════════════════════════════════════════════════════════════════
# WRITE ACTION LABELS (τι θα δει ο γιατρός να εγκρίνει)
# ════════════════════════════════════════════════════════════════════════════
def action_label(tool, inp):
    n = inp.get("name") or inp.get("patient_name") or inp.get("phone") or ""
    if tool == "book_appointment":
        return f"📅 Κλείσιμο ραντεβού: {n} — {inp.get('date')} {inp.get('time')}" \
               + (f" ({inp.get('service')})" if inp.get("service") else "")
    if tool == "cancel_appointment":
        return f"❌ Ακύρωση ραντεβού #{inp.get('booking_id')}" + (f" — {n}" if n else "")
    if tool == "reschedule_appointment":
        return f"🔁 Μετάθεση ραντεβού #{inp.get('booking_id')} → {inp.get('new_date')} {inp.get('new_time')}" \
               + (f" ({n})" if n else "")
    if tool == "create_invoice":
        return f"🧾 Τιμολόγιο: {n} — {inp.get('service')} €{inp.get('amount')}"
    if tool == "record_payment":
        amt = inp.get("amount")
        return f"💶 Καταγραφή πληρωμής τιμολογίου #{inp.get('invoice_id')}" \
               + (f" €{amt}" if amt else " (εξόφληση υπολοίπου)") + (f" — {n}" if n else "")
    if tool == "send_message":
        return f"✉️ Μήνυμα σε {n}: «{(inp.get('text') or '')[:60]}»"
    if tool == "add_patient":
        return f"➕ Νέος ασθενής: {n} ({inp.get('phone')})"
    if tool == "add_note":
        return f"📝 Σημείωση φακέλου: {n}"
    if tool == "winback":
        return f"🔄 Επανακλήσεις χαμένων ασθενών (αδράνεια {inp.get('days', 90)} ημ., έως {inp.get('limit', 20)})"
    return f"{tool}: {json.dumps(inp, ensure_ascii=False)}"


# ════════════════════════════════════════════════════════════════════════════
# IDEMPOTENCY — μία εντολή ≠ μία εκτέλεση. Κλειδί = CLIENT request_id (ένα ανά
# έγκριση, μοντέλο Stripe): ίδιο rid (network retry/double-tap) → cached· διαφορετικό
# rid → εκτελείται (δύο ΝΟΜΙΜΕΣ ίδιες ενέργειες ΔΕΝ μπλοκάρονται). Ατομικό claim μέσω
# UNIQUE insert (κλείνει το race check-then-act). Failure → release (retry επιτρεπτό).
# ════════════════════════════════════════════════════════════════════════════
def _idem_claim(slug, rid):
    """Ατομικό claim. Επιστρέφει {"status":"claimed"|"done"|"in_flight", "result": dict|None}."""
    try:
        with db._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS bo_idem(
                slug TEXT, key TEXT, status TEXT, result TEXT,
                created TEXT DEFAULT (datetime('now')), UNIQUE(slug, key))""")
            try:
                c.execute("INSERT INTO bo_idem(slug,key,status) VALUES(?,?,'in_flight')", (slug, rid))
                c.commit()
                return {"status": "claimed", "result": None}
            except Exception:  # UNIQUE → υπάρχει ήδη
                row = c.execute("SELECT status, result FROM bo_idem WHERE slug=? AND key=?",
                                (slug, rid)).fetchone()
                if row and row["status"] == "done" and row["result"]:
                    return {"status": "done", "result": json.loads(row["result"])}
                # in_flight· αν stale (>120s, π.χ. crash στη μέση) → take over
                stale = c.execute("SELECT 1 FROM bo_idem WHERE slug=? AND key=? "
                                  "AND created < datetime('now','-120 seconds')", (slug, rid)).fetchone()
                if stale:
                    c.execute("UPDATE bo_idem SET status='in_flight', created=datetime('now') "
                              "WHERE slug=? AND key=?", (slug, rid))
                    c.commit()
                    return {"status": "claimed", "result": None}
                return {"status": "in_flight", "result": None}
    except Exception:
        return {"status": "claimed", "result": None}  # idempotency fail-open → μην μπλοκάρεις legit


def _idem_finish(slug, rid, result):
    """success → αποθήκευσε result· failure → release claim (επιτρέπει retry)."""
    try:
        with db._conn() as c:
            if isinstance(result, dict) and result.get("ok"):
                c.execute("UPDATE bo_idem SET status='done', result=? WHERE slug=? AND key=?",
                          (json.dumps(result, ensure_ascii=False), slug, rid))
            else:
                c.execute("DELETE FROM bo_idem WHERE slug=? AND key=?", (slug, rid))
            c.commit()
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════
# WRITE ACTION EXECUTORS (τρέχουν ΜΟΝΟ μετά από έγκριση, μέσω /execute)
# ════════════════════════════════════════════════════════════════════════════
def execute_action(slug, tool, inp, request_id=None):
    """Εκτελεί ΜΙΑ εγκεκριμένη ενέργεια. Idempotent ΜΟΝΟ με request_id (client key ανά
    έγκριση): ίδιο rid → cached· διαφορετικό/κανένα → εκτελείται κανονικά."""
    if tool not in WRITE_TOOLS:
        return {"ok": False, "error": f"Μη επιτρεπτή ενέργεια: {tool}"}
    if not request_id:
        return _execute_action_raw(slug, tool, inp)
    claim = _idem_claim(slug, request_id)
    if claim["status"] == "done":
        return {**claim["result"], "idempotent": True}
    if claim["status"] == "in_flight":
        return {"ok": False, "error": "Η ίδια ενέργεια εκτελείται ήδη — περίμενε λίγο.", "in_flight": True}
    result = _execute_action_raw(slug, tool, inp)
    _idem_finish(slug, request_id, result)
    return result


def _execute_action_raw(slug, tool, inp):
    """Η πραγματική εκτέλεση. Επιστρέφει {ok, msg} ή {ok:False, error}."""
    if tool not in WRITE_TOOLS:
        return {"ok": False, "error": f"Μη επιτρεπτή ενέργεια: {tool}"}
    try:
        if tool == "book_appointment":
            biz = db.get_business(slug)
            dur = int(biz.get("slot_minutes") or 30) if biz else 30
            svc = inp.get("service")
            if not svc and biz and biz.get("services"):
                svc = (biz["services"][0] or {}).get("name") or "Ραντεβού"
            svc = svc or "Ραντεβού"
            bid = db.create_booking(slug, inp["date"], inp["time"], svc, dur,
                                    inp.get("name", ""), inp["phone"], inp.get("email", ""))
            bo.upsert_patient(slug, inp["phone"], inp.get("name", ""))
            return {"ok": True, "msg": f"Κλείστηκε ραντεβού #{bid} — {inp.get('name')} {inp['date']} {inp['time']}."}

        if tool == "cancel_appointment":
            db.cancel_booking(int(inp["booking_id"]))
            return {"ok": True, "msg": f"Ακυρώθηκε το ραντεβού #{inp['booking_id']}."}

        if tool == "reschedule_appointment":
            ok = db.reschedule_booking(int(inp["booking_id"]), inp["phone"],
                                       inp["new_date"], inp["new_time"])
            if ok:
                return {"ok": True, "msg": f"Μετατέθηκε σε {inp['new_date']} {inp['new_time']}."}
            return {"ok": False, "error": "Δεν βρέθηκε το ραντεβού (ή δεν ταιριάζει το τηλέφωνο)."}

        if tool == "create_invoice":
            from vertical_templates import vat_rate_for
            biz = db.get_business(slug)
            vat = vat_rate_for(biz["type"] if biz else "")
            iid = bo.create_invoice(slug, inp["phone"], inp.get("name", ""),
                                    inp.get("service", "Υπηρεσία"), float(inp["amount"]),
                                    vat_rate=vat, status="unpaid")
            return {"ok": True, "msg": f"Εκδόθηκε τιμολόγιο #{iid} (€{inp['amount']}, ΦΠΑ {vat}%)."}

        if tool == "record_payment":
            amt = inp.get("amount")
            if not amt:
                with db._conn() as c:
                    row = c.execute("SELECT total, paid FROM bo_invoices WHERE slug=? AND id=?",
                                    (slug, int(inp["invoice_id"]))).fetchone()
                if not row:
                    return {"ok": False, "error": "Το τιμολόγιο δεν βρέθηκε."}
                amt = round((row["total"] or 0) - (row["paid"] or 0), 2)
            res = bo.record_payment(slug, int(inp["invoice_id"]), float(amt))
            if res.get("ok"):
                return {"ok": True, "msg": f"Καταγράφηκε πληρωμή €{res.get('applied', amt)}."}
            return {"ok": False, "error": res.get("error", "Σφάλμα πληρωμής")}

        if tool == "send_message":
            phone = inp["phone"]
            text = (inp.get("text") or "").strip()
            email = ""
            with db._conn() as c:
                row = c.execute("SELECT email FROM bo_patients WHERE slug=? AND phone=?",
                                (slug, phone)).fetchone()
                email = (row["email"] if row else "") or ""
            channel = inp.get("channel")
            sent_via = None
            if (channel == "email" or not channel) and email:
                try:
                    from email_service import send_email_via_resend
                    biz = db.get_business(slug)
                    fn = biz["name"] if biz else "Ιατρείο"
                    if send_email_via_resend(email, "Ενημέρωση από το ιατρείο",
                                             f"<p>{text}</p>", from_name=fn):
                        sent_via = "email"
                except Exception as e:
                    print(f"[assistant send_message] email failed: {e}")
            if not sent_via and channel != "email":
                try:
                    from sms_service import send_sms
                    if send_sms(phone, text):
                        sent_via = "sms"
                except Exception as e:
                    print(f"[assistant send_message] sms failed: {e}")
            bo.log_comm(slug, phone, sent_via or "note", text, direction="out",
                        subject="Μήνυμα", status="sent" if sent_via else "queued")
            if sent_via:
                return {"ok": True, "msg": f"Στάλθηκε μήνυμα ({sent_via}) στον/στην {inp.get('name') or phone}."}
            # HONESTY (tribunal fix): ΜΗΝ πεις ok:True ενώ δεν έφυγε τίποτα (silent false-positive σε
            # healthcare = επικίνδυνο). Καταγράφηκε μόνο — δήλωσέ το ως αποτυχία αποστολής.
            return {"ok": False, "sent": False,
                    "error": (f"ΔΕΝ στάλθηκε στον/στην {inp.get('name') or phone} — δεν υπάρχει διαθέσιμο κανάλι "
                              f"({'χωρίς email· ' if not email else ''}SMS μη ρυθμισμένο). Καταγράφηκε μόνο στο ιστορικό. "
                              f"Βάλε email στον ασθενή ή ρύθμισε SMS για να φεύγουν.")}

        if tool == "add_patient":
            bo.upsert_patient(slug, inp["phone"], inp.get("name", ""), inp.get("email", ""))
            return {"ok": True, "msg": f"Προστέθηκε ασθενής: {inp.get('name')}."}

        if tool == "add_note":
            bo.add_patient_note(slug, inp["phone"], inp["body"], kind="clinical",
                                title=inp.get("title", "Σημείωση"), author="AI Γραμματέας")
            return {"ok": True, "msg": "Προστέθηκε σημείωση στον φάκελο."}

        if tool == "winback":
            from voice.outbound import enqueue_winback_for_inactive_customers
            n = enqueue_winback_for_inactive_customers(
                slug, days_inactive=int(inp.get("days", 90)), limit=int(inp.get("limit", 20)))
            return {"ok": True, "msg": f"Προγραμματίστηκαν {n} επανακλήσεις."}
    except ValueError as e:
        if "double_booking" in str(e):
            return {"ok": False, "error": "Η ώρα είναι πιασμένη — διάλεξε άλλη."}
        return {"ok": False, "error": str(e)[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": False, "error": "unknown_action"}


# ════════════════════════════════════════════════════════════════════════════
# AGENTIC LOOP — /ask
# ════════════════════════════════════════════════════════════════════════════
def _system_prompt(slug):
    biz = db.get_business(slug)
    name = biz["name"] if biz else slug
    services = ""
    if biz and biz.get("services"):
        services = "Υπηρεσίες: " + ", ".join(
            f"{(s.get('name') or '')}" + (f" (€{s.get('price')})" if s.get("price") else "")
            for s in biz["services"][:20])
    return (
        f"Είσαι η γραμματέας του ιατρείου «{name}». Μιλάς ελληνικά, ζεστά και απλά — σαν μια έμπειρη, "
        f"ευγενική κοπέλα στη ρεσεψιόν που ξέρει απ' έξω το ιατρείο και βοηθάει τον γιατρό με ό,τι χρειαστεί.\n"
        f"Κλείνεις και κανονίζεις ραντεβού, κρατάς τους φακέλους των ασθενών, βγάζεις τιμολόγια, "
        f"σημειώνεις πληρωμές, στέλνεις υπενθυμίσεις και απαντάς σε ό,τι σε ρωτήσει ο γιατρός για το ιατρείο.\n\n"
        f"Στην αρχή της κουβέντας σου λέω τη σημερινή ημερομηνία. Όταν ο γιατρός λέει «αύριο», «την Παρασκευή», "
        f"«σε μια βδομάδα», βρες μόνη σου την ακριβή ημερομηνία. {services}\n\n"
        f"Έχεις πρόσβαση σε όλα τα δεδομένα του ιατρείου μέσα από εργαλεία. Όταν σε ρωτάει κάτι που δεν το "
        f"καλύπτει κάποιο έτοιμο εργαλείο (δικά του στατιστικά, λίστες, αναφορές), έχεις και το query_data για "
        f"να ψάξεις ό,τι χρειαστεί — απλώς βάζε πάντα slug = '{slug}' στο ερώτημα. Προτίμα τα έτοιμα εργαλεία όταν ταιριάζουν.\n\n"
        "Πώς δουλεύεις:\n"
        "• Όταν ρωτάει κάτι (τι έχω σήμερα, ποιος χρωστάει, πώς πάμε, δείξε μου τον Γιώργο, τι να παραγγείλω): "
        "κοίτα τα δεδομένα και πες του απλά και σύντομα την απάντηση — με λίγα bullets, χωρίς φλυαρίες.\n"
        "• Πριν ακυρώσεις, μεταθέσεις ή τιμολογήσεις κάποιον, βρες πρώτα τον ασθενή για να 'χεις τα σωστά στοιχεία. "
        "Πριν κλείσεις ραντεβού σε γεμάτη μέρα, τσέκαρε τι ώρες είναι ελεύθερες.\n"
        "• Όταν θες να κάνεις μια αλλαγή (κλείσιμο/ακύρωση/τιμολόγιο/πληρωμή/μήνυμα), ετοίμασέ την και πες "
        "στον γιατρό τι θα γίνει μόλις πατήσει «Έγκριση». ΠΟΤΕ μην πεις πως κάτι έγινε ήδη — λες «θα γίνει μόλις το εγκρίνεις».\n"
        "• Αν σου λείπει κάτι βασικό (π.χ. το τηλέφωνο για νέο ραντεβού), ρώτα το ίσια — μην το μαντεύεις.\n\n"
        "Ο γιατρός δεν είναι τεχνικός, κάν' του τη ζωή εύκολη:\n"
        "• Καταλαβαίνεις greeklish και σπαστά ελληνικά χωρίς τόνους ή σημεία στίξης («kleise rantevou», «ποσα μου χρωστανε»).\n"
        "• Αν κάτι είναι ασαφές (π.χ. δύο ασθενείς με ίδιο όνομα), ρώτα ποιον εννοεί — επίθετο ή τηλέφωνο.\n"
        "• Αν σε ρωτήσει κάτι άσχετο με το ιατρείο, πες ευγενικά ότι είσαι η βοηθός του ιατρείου.\n"
        "• Ποτέ μην πεις ξερό «δεν κατάλαβα». Πάντα βοήθα: ρώτα διευκρίνιση ή πες τι μπορείς να κάνεις.\n"
        "• Όταν δεν υπάρχει κάτι, είναι καλό νέο — πες το θετικά («κανείς δεν χρωστάει αυτή τη στιγμή ✅»)."
    )


def run_assistant(slug, question, history=None, max_loops=8):
    """Επιστρέφει {answer, proposed_actions:[{tool,input,label}], needs_approval}.
    READ tools τρέχουν αυτόματα· WRITE tools συλλέγονται ως proposed actions."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    sys = _system_prompt(slug)
    # Η ημερομηνία ΕΞΩ από το cached prefix (αλλιώς το cache σπάει κάθε μέρα):
    # μπαίνει ως πρώτο user-turn context. Το system+tools μένουν σταθερά → cache hit.
    today = datetime.now()
    weekday_gr = ["Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή"][today.weekday()]
    messages = [{"role": "user", "content": f"(Σήμερα είναι {weekday_gr} {today.strftime('%d/%m/%Y')}.)"}]
    for h in (history or [])[-8:]:
        if h.get("role") in ("user", "assistant") and h.get("text"):
            messages.append({"role": h["role"], "content": h["text"]})
    messages.append({"role": "user", "content": question})

    # Prompt caching: το system + tools ξαναστέλνονται κάθε loop (~62% του κόστους).
    # cache_control:ephemeral → cached input = 10% του base (5-10x φθηνότερο).
    system_blocks = [{"type": "text", "text": sys, "cache_control": {"type": "ephemeral"}}]
    tools_cached = list(TOOLS)
    tools_cached[-1] = {**tools_cached[-1], "cache_control": {"type": "ephemeral"}}

    proposed = []
    answer_parts = []
    tools_ran = False
    for _ in range(max_loops):
        resp = client.messages.create(model=MODEL, max_tokens=1200, system=system_blocks,
                                       tools=tools_cached, messages=messages)
        # μάζεψε text
        for b in resp.content:
            if getattr(b, "type", "") == "text" and b.text:
                answer_parts.append(b.text)
        if resp.stop_reason != "tool_use":
            break
        # χτίσε assistant turn + tool_results
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for b in resp.content:
            if getattr(b, "type", "") != "tool_use":
                continue
            if b.name in READ_TOOLS:
                tools_ran = True
                res = _run_read_tool(slug, b.name, b.input or {})
                is_err = isinstance(res, dict) and "error" in res and len(res) == 1  # error-only dict
                tr = {"type": "tool_result", "tool_use_id": b.id,
                      "content": json.dumps(res, ensure_ascii=False)[:4000]}
                if is_err:
                    tr["is_error"] = True
                tool_results.append(tr)
            elif b.name in WRITE_TOOLS:
                inp = dict(b.input or {})
                proposed.append({"tool": b.name, "input": inp, "label": action_label(b.name, inp)})
                tool_results.append({"type": "tool_result", "tool_use_id": b.id,
                                     "content": "ΠΡΟΤΑΘΗΚΕ — αναμένει έγκριση του γιατρού. Μην το ξαναπροτείνεις."})
            else:
                tool_results.append({"type": "tool_result", "tool_use_id": b.id,
                                     "content": "unknown tool", "is_error": True})
        messages.append({"role": "user", "content": tool_results})

    answer = "\n".join(p.strip() for p in answer_parts if p.strip()).strip()
    # Εγγυημένη απάντηση: αν μάζεψε δεδομένα αλλά δεν συνέθεσε (εξάντλησε loops), κάνε
    # μία τελική κλήση ΧΩΡΙΣ tools ώστε να απαντήσει από όσα ήδη βρήκε.
    if not answer and tools_ran and not proposed:
        try:
            messages.append({"role": "user", "content":
                             "Απάντησε ΤΩΡΑ σύντομα στα ελληνικά με βάση τα δεδομένα που μάζεψες παραπάνω. Μην καλέσεις άλλο tool."})
            final = client.messages.create(model=MODEL, max_tokens=900, system=system_blocks, messages=messages)
            answer = "".join(getattr(b, "text", "") for b in final.content if getattr(b, "type", "") == "text").strip()
        except Exception as e:
            print(f"[assistant] final synthesis failed: {e}")
    if not answer:
        answer = "Έτοιμο — δες τις προτεινόμενες ενέργειες." if proposed else "Δεν κατάλαβα — μπορείς να το πεις αλλιώς;"
    return {"answer": answer, "proposed_actions": proposed, "needs_approval": bool(proposed)}
