"""
back_office_routes.py — Back-Office UI + API (μέσα στο ran-tevu.gr)

Ο γιατρός ανοίγει /<slug>/back-office?t=<token> (ίδιο HMAC token με το dashboard)
και βλέπει ΟΛΟ το laptop του ιατρείου: φάκελοι ασθενών, οικονομικά/myDATA, αποθήκη,
επικοινωνία, to-do, agents, και το Obsidian graph — όλα συνδεδεμένα.
"""
import os
import re
import hmac
from functools import wraps
from flask import Blueprint, request, jsonify, render_template_string, redirect

import database as db
import back_office as bo
from back_office_agents import get_back_office_status, back_office_agents_summary

bp = Blueprint("back_office", __name__)

# Δημόσιο sandbox demo (χωρίς token, read-only) — ο κριτής/επισκέπτης μπαίνει άμεσα
# σε ένα ιατρείο που «τρέχει μόνο του». Ρυθμιζόμενο slug μέσω env.
DEMO_SLUG = os.environ.get("DEMO_SLUG", "demo")


def _is_demo(slug):
    return slug == DEMO_SLUG

# Schema init (idempotent)
try:
    bo.init_back_office()
except Exception as _e:
    print(f"[BACK_OFFICE] init skipped: {_e}")


def require_master(fn):
    @wraps(fn)
    def wrap(*a, **k):
        token = (request.headers.get("Authorization", "").replace("Bearer ", "").strip()
                 or request.args.get("token", "").strip())
        admin = os.environ.get("ADMIN_TOKEN", "")
        if not admin or not token or not hmac.compare_digest(token, admin):  # fail-closed + constant-time
            return jsonify({"error": "unauthorized"}), 401
        return fn(*a, **k)
    return wrap


def _check_token(slug):
    """HMAC token (πελάτης/γιατρός) Ή owner cookie (Ντίνος μπαίνει παντού — και στο demo).
    ΣΚΟΠΙΜΑ ΧΩΡΙΣ bypass για το δημόσιο demo slug: το back-office/agents/δεδομένα δεν είναι
    δημόσια σε κανέναν επισκέπτη — μόνο ο owner (cookie) ή όποιος έχει το σωστό ?t= link."""
    try:
        from routes.start_routes import is_owner
        if is_owner():
            return True
    except Exception:
        pass
    try:
        from routes.dashboard_routes import dash_token
        import hmac
        return hmac.compare_digest(request.args.get("t", ""), dash_token(slug))
    except Exception:
        return False


def _premium_ok(slug):
    """Ο AI Γραμματέας είναι CORE feature — διαθέσιμος σε ΚΑΘΕ πελάτη (standard €179 + premium + demo).
    Η πρόσβαση φιλτράρεται ήδη από το _check_token (owner-only), οπότε εδώ δεν ξανα-gate-άρουμε ανά tier."""
    return True


# ── Owner-facing JSON (token-gated) ──────────────────────────────────────────
@bp.get("/api/<slug>/back-office/summary")
def bo_summary(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    return jsonify(bo.back_office_summary(slug))


@bp.get("/api/<slug>/back-office/channels")
def bo_channels(slug):
    """Ποια κανάλια ειδοποίησης είναι όντως ρυθμισμένα (email/SMS/voice) — ο γιατρός βλέπει
    αν τρέχει σε degraded mode χωρίς να ρωτήσει εσένα («γιατί δεν φεύγουν SMS;»)."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    import os as _os
    sms = bool(_os.environ.get("TWILIO_ACCOUNT_SID") and _os.environ.get("TWILIO_AUTH_TOKEN")
               and (_os.environ.get("TWILIO_PHONE_NUMBER") or _os.environ.get("TWILIO_DEFAULT_FROM")
                    or _os.environ.get("TWILIO_FROM_NUMBER")))
    return jsonify({
        "email": bool(_os.environ.get("RESEND_API_KEY")),
        "branded_email": bool(_os.environ.get("FROM_DOMAIN")),
        "sms": sms,
        "voice": bool(_os.environ.get("VAPI_API_KEY")),
    })


@bp.get("/api/<slug>/back-office/risk")
def bo_risk(slug):
    """Εξηγήσιμο no-show risk για τα επερχόμενα ραντεβού (7 ημέρες) — κάθε score με ΛΟΓΟΥΣ.
    Ο γιατρός βλέπει ΠΟΙΟΣ κινδυνεύει να μην έρθει και ΓΙΑΤΙ, πριν συμβεί."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    try:
        from risk_brain import scan_upcoming
        return jsonify(scan_upcoming(slug, request.args.get("days", 7, type=int)))
    except Exception as e:
        print(f"[RISK] error for {slug}: {e}")
        return jsonify({"scanned": 0, "high": [], "medium": [], "appointments": []}), 200


@bp.get("/api/<slug>/back-office/recall")
def bo_recall(slug):
    """Recall engine: ασθενείς που έπρεπε να έχουν ξανάρθει (χαμένο revenue), με € δυναμικό + λόγο."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    try:
        from risk_brain import recall_candidates
        return jsonify(recall_candidates(slug, request.args.get("days", 60, type=int)))
    except Exception as e:
        print(f"[RECALL] error for {slug}: {e}")
        return jsonify({"candidates": [], "total": 0, "eur_potential": 0}), 200


@bp.get("/api/<slug>/back-office/revenue-analytics")
def bo_revenue_analytics(slug):
    """€ ανά υπηρεσία + ημέρα εβδομάδας από πραγματικά τιμολόγια — τι αφήνει λεφτά και πότε."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    try:
        from risk_brain import revenue_analytics
        return jsonify(revenue_analytics(slug, request.args.get("days", 90, type=int)))
    except Exception as e:
        print(f"[REV-ANALYTICS] error for {slug}: {e}")
        return jsonify({"by_service": [], "by_weekday": [], "total_eur": 0}), 200


@bp.get("/api/<slug>/back-office/forecast")
def bo_forecast(slug):
    """Πρόβλεψη ζήτησης ραντεβού (TimesFM όταν είναι εγκατεστημένο, στατιστικό fallback αλλιώς).
    Θέλει ≥14 μέρες πραγματικού ιστορικού — με λιγότερα επιστρέφει honest ready:false."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    try:
        from forecast import forecast_demand
        horizon = request.args.get("days", 14, type=int)
        return jsonify(forecast_demand(slug, horizon))
    except Exception as e:
        print(f"[FORECAST] error for {slug}: {e}")
        return jsonify({"ready": False, "reason": "forecast_unavailable"}), 200


@bp.get("/api/<slug>/back-office/patients")
def bo_patients(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    with db._conn() as c:
        rows = c.execute("""SELECT p.phone, p.name, p.email, p.status,
                                   (SELECT COUNT(*) FROM bookings b WHERE b.slug=p.slug AND b.phone=p.phone) AS visits,
                                   (SELECT MAX(date) FROM bookings b WHERE b.slug=p.slug AND b.phone=p.phone) AS last_visit,
                                   (SELECT COALESCE(SUM(total-paid),0) FROM bo_invoices i WHERE i.slug=p.slug AND i.patient_phone=p.phone AND i.status!='paid') AS balance
                            FROM bo_patients p WHERE p.slug=? ORDER BY last_visit DESC""", (slug,)).fetchall()
    return jsonify({"patients": [dict(r) for r in rows]})


@bp.get("/api/<slug>/back-office/patient/<phone>")
def bo_patient(slug, phone):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    return jsonify(bo.patient_360(slug, phone))


@bp.get("/api/<slug>/back-office/finance")
def bo_finance(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    with db._conn() as c:
        invs = c.execute("""SELECT id, number, issue_date, patient_name, service, total, paid, status, mydata_status
                            FROM bo_invoices WHERE slug=? ORDER BY issue_date DESC LIMIT 100""", (slug,)).fetchall()
    return jsonify({"summary": bo.finance_summary(slug),
                    "outstanding": bo.outstanding_balances(slug),
                    "invoices": [dict(r) for r in invs]})


@bp.get("/api/<slug>/back-office/recovery")
def bo_recovery(slug):
    """Recovered-Revenue Ledger: € won back from lost patients (proof-of-value)."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    days = request.args.get("days", default=30, type=int)
    return jsonify({"summary": db.get_recovery_summary(slug, days),
                    "ledger": db.get_recovery_ledger(slug, days)})


@bp.get("/api/<slug>/back-office/inventory")
def bo_inventory(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    with db._conn() as c:
        items = c.execute("""SELECT i.id, i.name, i.category, i.qty, i.unit, i.reorder_level, i.unit_cost,
                                   s.name AS supplier FROM bo_inventory i
                            LEFT JOIN bo_suppliers s ON s.id=i.supplier_id AND s.slug=i.slug
                            WHERE i.slug=? ORDER BY i.qty""", (slug,)).fetchall()
        sups = c.execute("SELECT id, name, category, contact, phone, email FROM bo_suppliers WHERE slug=?", (slug,)).fetchall()
    return jsonify({"items": [dict(r) for r in items], "suppliers": [dict(r) for r in sups],
                    "low_stock": bo.low_stock(slug)})


@bp.get("/api/<slug>/back-office/tasks")
def bo_tasks(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    with db._conn() as c:
        rows = c.execute("""SELECT id, title, kind, priority, related_type, related_id, due, status
                            FROM bo_tasks WHERE slug=? AND status='open'
                            ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id""",
                         (slug,)).fetchall()
    return jsonify({"tasks": [dict(r) for r in rows]})


@bp.get("/api/<slug>/back-office/agents")
def bo_agents(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    return jsonify(back_office_agents_summary(slug))


# ── Owner ACTIONS (γράψιμο — token/owner-gated) ──────────────────────────────
def _body():
    return request.get_json(silent=True, force=True) or {}


@bp.post("/api/<slug>/back-office/patient")
def bo_add_patient(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    d = _body()
    phone = (d.get("phone") or "").strip()[:40]
    if not phone:
        return jsonify({"error": "Το τηλέφωνο είναι υποχρεωτικό"}), 400
    bo.upsert_patient(slug, phone, (d.get("name") or "").strip()[:120],
                      (d.get("email") or "").strip()[:200],
                      medical_alerts=(d.get("medical_alerts") or "").strip()[:500])
    return jsonify({"ok": True, "phone": phone})


@bp.post("/api/<slug>/back-office/note")
def bo_add_note(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    d = _body()
    phone = (d.get("phone") or "").strip()[:40]
    body = (d.get("body") or "").strip()[:5000]
    if not phone or not body:
        return jsonify({"error": "Λείπει ασθενής ή κείμενο"}), 400
    nid = bo.add_patient_note(slug, phone, body, kind="clinical",
                             title=(d.get("title") or "Σημείωση")[:120], author="Ιατρός")
    return jsonify({"ok": True, "id": nid})


@bp.post("/api/<slug>/back-office/invoice")
def bo_create_invoice(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    from vertical_templates import vat_rate_for
    d = _body()
    phone = (d.get("phone") or "").strip()[:40]
    try:
        amount = float(d.get("amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Μη έγκυρο ποσό"}), 400
    if amount <= 0:
        return jsonify({"error": "Το ποσό πρέπει να είναι > 0"}), 400
    biz = db.get_business(slug)
    vat = vat_rate_for(biz["type"] if biz else "")
    iid = bo.create_invoice(slug, phone, (d.get("name") or "").strip()[:120],
                            (d.get("service") or "Υπηρεσία").strip()[:120], amount,
                            vat_rate=vat, status="unpaid")
    return jsonify({"ok": True, "id": iid, "vat_rate": vat})


@bp.post("/api/<slug>/back-office/payment")
def bo_record_payment(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    d = _body()
    try:
        iid = int(d.get("invoice_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Μη έγκυρο τιμολόγιο"}), 400
    # default: εξόφληση του υπολοίπου
    with db._conn() as c:
        row = c.execute("SELECT total, paid FROM bo_invoices WHERE slug=? AND id=?", (slug, iid)).fetchone()
    if not row:
        return jsonify({"error": "Το τιμολόγιο δεν βρέθηκε"}), 404
    amount = d.get("amount")
    amount = float(amount) if amount else round((row["total"] or 0) - (row["paid"] or 0), 2)
    res = bo.record_payment(slug, iid, amount, method=(d.get("method") or "cash"))
    return (jsonify(res), 200) if res.get("ok") else (jsonify(res), 400)


@bp.post("/api/<slug>/back-office/task-done")
def bo_task_done(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    d = _body()
    try:
        tid = int(d.get("task_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Μη έγκυρη εργασία"}), 400
    if not bo.complete_task(slug, tid):
        return jsonify({"ok": False, "error": "Η εργασία δεν βρέθηκε"}), 404
    return jsonify({"ok": True})


# ── Admin-only ops ───────────────────────────────────────────────────────────
@bp.get("/api/admin/back-office/<slug>/run")
@require_master
def bo_run(slug):
    bo.init_back_office()
    return jsonify(bo.run_all_agents(slug))


@bp.get("/api/admin/back-office/<slug>/seed")
@require_master
def bo_seed(slug):
    """Γεμίζει realistic demo back-office (μία πηγή αλήθειας: bo.seed_demo_back_office)."""
    return jsonify(bo.seed_demo_back_office(slug))


@bp.get("/api/admin/back-office-agents/<slug>")
@require_master
def bo_admin_agents(slug):
    return jsonify(back_office_agents_summary(slug))


# ── Onboarding wizard (self-serve setup ωρών + υπηρεσιών) ─────────────────────
_GR_DAYS = ["Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή"]


@bp.get("/api/<slug>/setup-data")
def bo_setup_data(slug):
    """Τρέχουσα config για να προσυμπληρωθεί ο wizard."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    biz = db.get_business(slug)
    if not biz:
        return jsonify({"error": "not found"}), 404
    try:
        from calendar_service import service_account_email, auth_mode
        sa_email = service_account_email()
        cal_mode = auth_mode()
    except Exception:
        sa_email, cal_mode = "", "none"
    return jsonify({
        "name": biz.get("name", ""),
        "day_start": biz.get("day_start") or "09:00",
        "day_end": biz.get("day_end") or "21:00",
        "slot_minutes": biz.get("slot_minutes") or 30,
        "closed_days": biz.get("closed_days") or [6],
        "services": biz.get("services") or [],
        "google_calendar_id": biz.get("google_calendar_id") or "",
        "calendar_share_email": sa_email,
        "calendar_auth_mode": cal_mode,
        "briefing_enabled": int(biz.get("briefing_enabled", 1) or 0),
        "briefing_channel": biz.get("briefing_channel") or "auto",
        # εξατομίκευση φωνής
        "voice_assistant_name": biz.get("voice_assistant_name") or "Άννα",
        "voice_tone": biz.get("voice_tone") or "friendly",
        "voice_key": biz.get("voice_key") or "madlen",
        "voice_greeting": biz.get("voice_greeting") or "",
        "voice_custom_instructions": biz.get("voice_custom_instructions") or "",
        "days": _GR_DAYS,
    })


@bp.post("/api/<slug>/setup")
def bo_setup_save(slug):
    """Αποθηκεύει ώρες + υπηρεσίες/τιμές από τον wizard. Owner/token-gated."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    if not db.get_business(slug):
        return jsonify({"error": "not found"}), 404
    d = _body()
    # Ώρες
    ds = (d.get("day_start") or "09:00").strip()[:5]
    de = (d.get("day_end") or "21:00").strip()[:5]
    try:
        slot = max(5, min(180, int(d.get("slot_minutes") or 30)))
    except (TypeError, ValueError):
        slot = 30
    closed = [int(x) for x in (d.get("closed_days") or []) if str(x).isdigit() and 0 <= int(x) <= 6]
    # Υπηρεσίες
    services = []
    for s in (d.get("services") or [])[:40]:
        nm = (s.get("name") or "").strip()[:120]
        if not nm:
            continue
        try:
            price = round(float(s.get("price") or 0), 2)
        except (TypeError, ValueError):
            price = 0
        try:
            dur = max(5, min(240, int(s.get("duration") or slot)))
        except (TypeError, ValueError):
            dur = slot
        services.append({"name": nm, "price": price, "duration": dur})
    if not services:
        return jsonify({"error": "Πρόσθεσε τουλάχιστον μία υπηρεσία με τιμή."}), 400
    # hours dict (συνεπές με get_day_hours) — κλειστές μέρες ή day_start-day_end
    hours = {_GR_DAYS[i]: ("Κλειστά" if i in closed else f"{ds} - {de}") for i in range(7)}
    cfg = {"day_start": ds, "day_end": de, "slot_minutes": slot,
           "closed_days": closed, "services": services, "hours": hours}
    if "google_calendar_id" in d:   # σύνδεση Google Calendar (context)
        cfg["google_calendar_id"] = (d.get("google_calendar_id") or "").strip()[:200]
    if "briefing_enabled" in d:      # ημερήσια ενημέρωση γιατρού (on/off)
        cfg["briefing_enabled"] = 1 if d.get("briefing_enabled") else 0
    if "briefing_channel" in d:      # auto / email / sms / voice
        ch = (d.get("briefing_channel") or "auto").strip().lower()
        cfg["briefing_channel"] = ch if ch in ("auto", "email", "sms", "voice") else "auto"
    # ── Εξατομίκευση φωνής (η ΔΙΚΗ ΤΟΥ γραμματέα) — μπαίνουν στο Vapi assistant ──
    if "voice_assistant_name" in d:
        cfg["voice_assistant_name"] = (d.get("voice_assistant_name") or "").strip()[:40] or "Άννα"
    if "voice_tone" in d:
        vt = (d.get("voice_tone") or "friendly").strip().lower()
        cfg["voice_tone"] = vt if vt in ("formal", "friendly", "casual", "energetic", "professional") else "friendly"
    if "voice_key" in d:
        vk = (d.get("voice_key") or "").strip().lower()
        if vk in ("madlen", "athina", "stefanos", "dimitris"):
            cfg["voice_key"] = vk
    if "voice_greeting" in d:
        cfg["voice_greeting"] = (d.get("voice_greeting") or "").strip()[:300]
    if "voice_custom_instructions" in d:
        cfg["voice_custom_instructions"] = (d.get("voice_custom_instructions") or "").strip()[:1500]
    db.update_business(slug, cfg)
    return jsonify({"ok": True, "services": len(services),
                    "msg": f"Αποθηκεύτηκαν {len(services)} υπηρεσίες + ωράριο."})


@bp.get("/api/<slug>/back-office/today")
def bo_today(slug):
    """PROACTIVE money panel — «Σήμερα: X κινήσεις = €Y πίσω». Ο πυρήνας αξίας του back office."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    if not db.get_business(slug):
        return jsonify({"error": "not found"}), 404
    return jsonify(bo.money_actions(slug))


@bp.get("/api/<slug>/back-office/automations")
def bo_automations(slug):
    """Τι έκανε ο AI βοηθός μόνος του (visibility στην αυτοματοποίηση) + manual actions."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    if not db.get_business(slug):
        return jsonify({"error": "not found"}), 404
    days = 7
    try:
        days = max(1, min(90, int(request.args.get("days", 7))))
    except (TypeError, ValueError):
        days = 7
    act = bo.automation_activity(slug, days)
    act["actions"] = bo.money_actions(slug).get("actions", [])   # «ή κάνε το τώρα»
    return jsonify(act)


@bp.post("/api/<slug>/back-office/run-action")
def bo_run_action(slug):
    """Εκτελεί ΜΙΑ money action με ένα κλικ (winback / collect)."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    if not db.get_business(slug):
        return jsonify({"error": "not found"}), 404
    key = (_body().get("key") or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "Λείπει το key της ενέργειας."}), 400
    try:
        return jsonify(bo.run_money_action(slug, key))
    except Exception as e:
        return jsonify({"ok": False, "error": f"Σφάλμα: {str(e)[:200]}"}), 500


@bp.get("/api/<slug>/back-office/day")
def bo_day(slug):
    """Day board — σημερινά ραντεβού (check-in/no-show) + ταμείο ημέρας + αυριανά."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    if not db.get_business(slug):
        return jsonify({"error": "not found"}), 404
    raw = (request.args.get("date") or "").strip()[:10]
    day = raw if re.match(r"^\d{4}-\d{2}-\d{2}$", raw) else None  # αλλιώς → σήμερα (όχι false-empty)
    return jsonify(bo.day_board(slug, day))


@bp.post("/api/<slug>/back-office/booking-outcome")
def bo_booking_outcome(slug):
    """«✓ Ήρθε» (completed) ή «✕ Δεν ήρθε» (no_show) — με ένα κλικ από τη Day board."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    d = _body()
    res = bo.resolve_booking(slug, d.get("booking_id"), (d.get("outcome") or "").strip())
    return (jsonify(res), 200) if res.get("ok") else (jsonify(res), 400)


@bp.post("/api/<slug>/back-office/confirm-tomorrow")
def bo_confirm_tomorrow(slug):
    """Στέλνει επιβεβαίωση σε όλα τα αυριανά ραντεβού (μείωση no-show)."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    if not db.get_business(slug):
        return jsonify({"error": "not found"}), 404
    try:
        r = bo.confirm_tomorrow(slug)
        if r["total"] == 0:
            r["msg"] = "Δεν υπάρχουν αυριανά ραντεβού προς επιβεβαίωση."
        elif r["sent"]:
            r["msg"] = f"Στάλθηκαν {r['sent']} επιβεβαιώσεις" + (f" ({r['queued']} σε αναμονή)." if r["queued"] else ".")
        else:
            r["msg"] = f"Ετοιμάστηκαν {r['queued']} επιβεβαιώσεις — θα φύγουν μόλις συνδέσεις SMS ή email."
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Σφάλμα: {str(e)[:200]}"}), 500


@bp.get("/api/<slug>/back-office/waitlist")
def bo_waitlist_list(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    return jsonify({"waitlist": bo.list_waitlist(slug)})


@bp.post("/api/<slug>/back-office/waitlist")
def bo_waitlist_add(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    d = _body()
    res = bo.add_waitlist(slug, (d.get("name") or "").strip(), (d.get("phone") or "").strip(),
                          (d.get("service") or "").strip(), (d.get("note") or "").strip())
    return (jsonify(res), 200) if res.get("ok") else (jsonify(res), 400)


@bp.post("/api/<slug>/back-office/waitlist-remove")
def bo_waitlist_remove(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    try:
        wid = int(_body().get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Μη έγκυρο id"}), 400
    return jsonify(bo.remove_waitlist(slug, wid))


@bp.post("/api/<slug>/back-office/waitlist-notify")
def bo_waitlist_notify(slug):
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    try:
        wid = int(_body().get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Μη έγκυρο id"}), 400
    res = bo.notify_waitlist(slug, wid)
    return (jsonify(res), 200) if res.get("ok") else (jsonify(res), 400)


@bp.get("/api/<slug>/back-office/briefing")
def bo_briefing_preview(slug):
    """Preview της ημερήσιας ενημέρωσης (τι θα δει/ακούσει ο γιατρός) — για το UI."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    if not db.get_business(slug):
        return jsonify({"error": "not found"}), 404
    try:
        import briefing
        b = briefing.build(slug)
        return jsonify({"ok": True, "subject": b["subject"], "sms": b["sms"],
                        "html": b["html"], "has_content": b["has_content"]})
    except Exception as e:
        return jsonify({"error": "briefing_failed", "detail": str(e)[:200]}), 500


@bp.post("/api/<slug>/back-office/briefing/send")
def bo_briefing_send(slug):
    """Χειροκίνητο «στείλε μου τη σύνοψη τώρα» — στέλνει στο κανάλι του γιατρού."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    if not db.get_business(slug):
        return jsonify({"error": "not found"}), 404
    try:
        import briefing
        r = briefing.send(slug, force=True)
        if r.get("sent"):
            ch = {"email": "email", "sms": "SMS", "voice_queued": "φωνητική κλήση"}.get(
                r.get("channel"), r.get("channel"))
            return jsonify({"ok": True, "msg": f"Στάλθηκε η ενημέρωση ({ch}).", "channel": r.get("channel")})
        return jsonify({"ok": False, "msg": "Δεν βρέθηκε κανάλι — βάλε owner_email ή τηλέφωνο στις ρυθμίσεις."})
    except Exception as e:
        return jsonify({"error": "briefing_failed", "detail": str(e)[:200]}), 500


@bp.post("/api/<slug>/load-sample")
def bo_load_sample(slug):
    """Φορτώνει ρεαλιστικό ΔΕΙΓΜΑ δεδομένων ώστε να φανεί πώς δουλεύει το back office + ο AI Βοηθός.
    Ελεγχόμενο από τον χρήστη (όχι μόνιμο fake). Καθαρίζεται με /reset-data."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    if not db.get_business(slug):
        return jsonify({"error": "not found"}), 404
    try:
        import provisioning
        res = provisioning.load_sample_data(slug)
        if res.get("error"):
            return jsonify({"ok": False, "error": res["error"]}), 400
        return jsonify({"ok": True, **res,
                        "msg": "Φορτώθηκε δείγμα. Δες τον 🤖 Βοηθό & τις καρτέλες. "
                               "Πάτα «🗑️ Καθάρισε» όταν βάλεις τα δικά σου δεδομένα."})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Σφάλμα: {str(e)[:200]}"}), 500


@bp.post("/api/<slug>/reset-data")
def bo_reset_data(slug):
    """Μηδενίζει ΟΛΑ τα δεδομένα του ιατρείου (κρατά υπηρεσίες/ώρες). Για καθάρισμα δείγματος
    ή φρέσκο ξεκίνημα πραγματικού πελάτη."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    if not db.get_business(slug):
        return jsonify({"error": "not found"}), 404
    try:
        import provisioning
        res = provisioning.reset_business_data(slug)
        return jsonify({"ok": True, **res,
                        "msg": "Καθαρίστηκαν όλα — έτοιμο για τα δικά σου δεδομένα (Excel import ή χρήση)."})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Σφάλμα: {str(e)[:200]}"}), 500


@bp.post("/api/<slug>/provision-voice")
def bo_provision_voice(slug):
    """1-CLICK voice setup: φτιάχνει AI assistant με τα στοιχεία ΤΟΥ ιατρείου (υπηρεσίες/ώρες/όνομα),
    του δίνει νούμερο (δωρεάν Vapi US — ΧΩΡΙΣ Twilio), τον δένει με το ιατρείο (webhook→slug).
    Graceful: αν λείπει VAPI_API_KEY, καθαρό μήνυμα αντί για crash."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    biz = db.get_business(slug)
    if not biz:
        return jsonify({"error": "not found"}), 404
    try:
        from voice import vapi_client
    except Exception as e:
        return jsonify({"ok": False, "error": f"Voice module μη διαθέσιμο: {str(e)[:120]}"}), 500
    if not vapi_client.is_configured():
        return jsonify({"ok": False, "needs_key": True,
                        "error": "Λείπει το VAPI_API_KEY. Βάλ' το στο Railway (vapi.ai → Dashboard → API Keys) "
                                 "και ξαναπάτα «Στήσε φωνή»."}), 400
    try:
        from voice.provisioner import provision_voice_agent, ProvisionError
        d = _body()
        sip_number = (d.get("sip_number") or "").strip() or None  # ελληνικό DID (+30…) αν δοθεί
        # «1 κουμπί» = η ΔΙΚΗ ΤΟΥ γραμματέα: χρησιμοποίησε τα αποθηκευμένα στοιχεία
        # (όνομα/φωνή/ύφος/χαιρετισμός/οδηγίες) που μάζεψε η φόρμα setup.
        res = provision_voice_agent(
            business_slug=slug,
            assistant_name=(d.get("assistant_name") or biz.get("voice_assistant_name") or "Άννα")[:40],
            voice_key=(d.get("voice_key") or biz.get("voice_key") or None),
            twilio_phone=(d.get("twilio_phone") or None),
            sip_number=sip_number,
            purchase_new_number=bool(d.get("purchase_new_number")),
        )
        phone = res.get("twilio_phone_number") or "(θα ενεργοποιηθεί σε λίγο)"
        is_gr = bool(sip_number)
        note = ("Ελληνικό νούμερο ✅ — μηδέν διεθνής χρέωση. Κάνε προώθηση το τηλέφωνο του ιατρείου εδώ."
                if is_gr else
                "⚠️ US νούμερο (για test). Για production βάλε ελληνικό αριθμό — αλλιώς χρεώνεται διεθνώς η προώθηση.")
        return jsonify({"ok": True, "phone": phone, "is_greek": is_gr,
                        "assistant_id": res.get("vapi_assistant_id"),
                        "webhook_url": res.get("webhook_url"),
                        "msg": f"Έτοιμη η φωνή! 📞 {phone}. {note}"})
    except ProvisionError as e:
        return jsonify({"ok": False, "error": f"Δεν στήθηκε η φωνή: {str(e)[:200]}"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Σφάλμα: {str(e)[:200]}"}), 500


@bp.get("/<slug>/app-manifest.json")
def bo_app_manifest(slug):
    """Per-business PWA manifest — κάθε ιατρείο εγκαθιστά το ΔΙΚΟ του app που ανοίγει
    κατευθείαν στο δικό του back office (start_url = /<slug>/back-office). «App σε κάθε επιχείρηση»."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    biz = db.get_business(slug)
    if not biz:
        return jsonify({"error": "not found"}), 404
    t = request.args.get("t", "")
    name = biz.get("name") or slug
    start = f"/{slug}/back-office?t={t}" if t else f"/{slug}/back-office"
    return jsonify({
        "name": f"{name} — Ran-Tevu",
        "short_name": (name[:18] or "Ran-Tevu"),
        "id": f"/{slug}/back-office",
        "start_url": start,
        "scope": f"/{slug}/",
        "display": "standalone",
        "orientation": "portrait",
        "theme_color": "#6E56CF",
        "background_color": "#FAFAFB",
        "lang": "el",
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icons/icon-192-maskable.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/icons/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    })


@bp.get("/<slug>/setup")
def bo_setup_page(slug):
    if not _check_token(slug):
        return "<h2 style='font-family:sans-serif;padding:40px'>403 — Μη έγκυρο link.</h2>", 403
    biz = db.get_business(slug)
    if not biz:
        return "<h2 style='font-family:sans-serif;padding:40px'>404</h2>", 404
    return render_template_string(_SETUP_HTML, slug=slug, biz_name=biz.get("name", slug),
                                  t=request.args.get("t", ""))


# ── Owner HTML page (μέσα στο ran-tevu.gr) ───────────────────────────────────
@bp.get("/<slug>/back-office")
def back_office_page(slug):
    if not _check_token(slug):
        return "<h2 style='font-family:sans-serif;padding:40px'>403 — Μη έγκυρο link. Ζήτησε νέο σύνδεσμο.</h2>", 403
    try:
        from routes.billing_routes import subscription_blocked, paywall_response
        if not _is_demo(slug) and subscription_blocked(slug):   # το demo δεν μπλοκάρεται από paywall
            return paywall_response()
    except Exception:
        pass
    biz = db.get_business(slug)
    name = biz["name"] if biz else slug
    return render_template_string(_BO_HTML, slug=slug, biz_name=name, t=request.args.get("t", ""))


@bp.get("/demo")
def public_demo():
    """Δημόσιο sandbox demo — ο κριτής/επισκέπτης μπαίνει σε ένα ιατρείο που τρέχει μόνο του.
    Idempotent: στήνει + seed-άρει το demo slug την 1η φορά (physio, πλήρη δεδομένα),
    μετά redirect στο back-office. Καμία εγγραφή/token δεν χρειάζεται."""
    try:
        import provisioning
        provisioning.provision(
            name="Φυσικοθεραπευτήριο «Ευεξία»",
            vertical_key="physio",
            owner_name="Demo",
            email="demo@ran-tevu.gr",
            demo=True,
            slug=DEMO_SLUG,
        )
    except Exception as e:
        print(f"[DEMO] provision failed: {e}")
    return redirect(f"/{DEMO_SLUG}/back-office", code=302)


@bp.post("/api/<slug>/back-office/ask")
def back_office_ask(slug):
    """AI Γραμματέας (tool-calling, human-in-the-loop). Ο γιατρός γράφει φυσική γλώσσα·
    το AI ΣΧΕΔΙΑΖΕΙ τις ενέργειες (κλείσιμο/ακύρωση ραντεβού, τιμολόγια, μηνύματα…),
    τρέχει μόνο αναζητήσεις (READ) αυτόματα, και επιστρέφει τις αλλαγές ως
    proposed_actions προς ΕΓΚΡΙΣΗ — ΠΟΤΕ δεν εκτελεί αλλαγή μόνο του."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    q = (data.get("question") or "").strip()
    if not q:
        return jsonify({"error": "no_question"}), 400
    if not _premium_ok(slug):
        return jsonify({"answer": "🔒 Ο AI Γραμματέας (γράφεις φυσική γλώσσα → εκτελεί) είναι "
                                  "feature του πακέτου Premium (€299/μήνα). Με το Standard έχεις τα "
                                  "κουμπιά & φόρμες του back-office. Αναβάθμισε για να του μιλάς σαν "
                                  "σε γραμματέα.", "needs_approval": False,
                        "proposed_actions": [], "upsell": "premium"})
    try:
        import back_office_assistant as assistant
        result = assistant.run_assistant(slug, q, history=data.get("history"))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": "ai_unavailable", "detail": str(e)[:200]}), 500


@bp.post("/api/<slug>/back-office/execute")
def back_office_execute(slug):
    """Εκτελεί ΕΓΚΕΚΡΙΜΕΝΕΣ ενέργειες. Καλείται ΜΟΝΟ μετά από ρητή έγκριση του γιατρού.
    Δύο μορφές:
      • {actions:[{tool,input}]}  → οι προτάσεις του AI Γραμματέα (νέο)
      • {action:"winback|rebuild_tasks|run_agents"} → quick agent actions (legacy)."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 401
    if _is_demo(slug):        # sandbox: ο AI προτείνει, αλλά ΤΙΠΟΤΑ δεν εκτελείται/στέλνεται στο demo
        return jsonify({"ok": False, "demo": True,
                        "msg": "🧪 Demo — εδώ βλέπεις τι ΘΑ έκανε ο agent. Σε πραγματικό ιατρείο εκτελείται & στέλνεται αυτόματα."})
    d = request.get_json(silent=True) or {}

    # ── Νέο: λίστα εγκεκριμένων ενεργειών από τον AI Γραμματέα ────────────────
    actions = d.get("actions")
    if actions:
        if not _premium_ok(slug):
            return jsonify({"ok": False, "error": "Ο AI Γραμματέας είναι feature του Premium (€299).",
                            "upsell": "premium"}), 402
        import back_office_assistant as assistant
        results = []
        for a in actions[:20]:
            tool = (a.get("tool") or "").strip()
            res = assistant.execute_action(slug, tool, a.get("input") or {},
                                           request_id=a.get("aid"))
            results.append({"tool": tool, **res})
        ok_all = all(r.get("ok") for r in results)
        return jsonify({"ok": ok_all, "results": results,
                        "msg": " · ".join(r.get("msg") or r.get("error", "") for r in results)})

    # ── Legacy: μονές quick actions (κουμπιά agents) ──────────────────────────
    action = (d.get("action") or "").strip()
    try:
        if action == "winback":
            from voice.outbound import enqueue_winback_for_inactive_customers
            n = enqueue_winback_for_inactive_customers(
                slug, days_inactive=int(d.get("days", 90)), limit=int(d.get("limit", 20)))
            return jsonify({"ok": True, "action": action, "enqueued": n,
                            "msg": f"Προγραμματίστηκαν {n} επανακλήσεις χαμένων ασθενών (θα φύγουν αυτόματα)."})
        if action == "rebuild_tasks":
            bo.rebuild_tasks(slug)
            return jsonify({"ok": True, "action": action, "msg": "Οι εργασίες ανανεώθηκαν."})
        if action == "run_agents":
            bo.run_all_agents(slug)
            return jsonify({"ok": True, "action": action, "msg": "Οι agents έτρεξαν (τιμολόγια/αποθήκη/recall ενημερώθηκαν)."})
        return jsonify({"error": "unknown_action", "allowed": ["winback", "rebuild_tasks", "run_agents"]}), 400
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@bp.post("/api/<slug>/back-office/import-patients")
def bo_import_patients(slug):
    """Εισαγωγή παλιών ασθενών από Excel/CSV. Δέχεται είτε αρχείο (multipart 'file')
    είτε JSON {rows:[{name,phone,email,...}]}. Αναγνωρίζει ελληνικές/αγγλικές
    επικεφαλίδες. Κάθε γραμμή → upsert φάκελος ασθενή (idempotent στο τηλέφωνο)."""
    if not _check_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    rows = []
    f = request.files.get("file")
    if f:
        try:
            rows = _parse_patient_file(f)
        except Exception as e:
            return jsonify({"error": f"Δεν διαβάστηκε το αρχείο: {str(e)[:150]}"}), 400
    else:
        rows = (request.get_json(silent=True) or {}).get("rows") or []
    if not rows:
        return jsonify({"error": "Δεν βρέθηκαν γραμμές. Στείλε .xlsx/.csv με στήλες όνομα + τηλέφωνο."}), 400

    # import_patients: φάκελος + ιστορικό-booking (ΚΡΙΣΙΜΟ — το win-back ψάχνει στο bookings)
    res = bo.import_patients(slug, rows[:5000])
    msg = f"Εισήχθησαν {res['imported']} ασθενείς"
    if res["with_history"]:
        msg += (f" — {res['with_history']} με ιστορικό επίσκεψης, "
                f"{res['dormant_90']} ανενεργοί >90 ημ. (έτοιμοι για win-back)")
    msg += "."
    return jsonify({"ok": True, **res, "msg": msg})


def _parse_patient_file(f):
    """Parse .xlsx ή .csv → list of {name, phone, email}. Header-aware (GR/EN)."""
    fn = (f.filename or "").lower()
    raw = f.read()
    # column aliases
    name_keys  = {"name", "όνομα", "ονομα", "ονοματεπώνυμο", "ονοματεπωνυμο", "πελάτης", "πελατης", "ασθενής", "ασθενης", "fullname", "full name"}
    phone_keys = {"phone", "τηλέφωνο", "τηλεφωνο", "τηλ", "κινητό", "κινητο", "mobile", "tel", "telephone"}
    email_keys = {"email", "e-mail", "mail", "ηλεκτρονικό ταχυδρομείο", "ημαιλ"}
    # last_visit = ΚΡΙΣΙΜΟ για win-back (ποιος είναι ανενεργός). Προαιρετική στήλη.
    visit_keys = {"last_visit", "τελευταία επίσκεψη", "τελευταια επισκεψη", "τελ. επίσκεψη", "επίσκεψη", "επισκεψη",
                  "last visit", "τελευταίο ραντεβού", "τελευταιο ραντεβου", "ραντεβού", "ραντεβου", "ημερομηνία", "ημερομηνια", "date"}

    def _vstr(v):
        """datetime (από xlsx) → 'YYYY-MM-DD', αλλιώς string."""
        try:
            return v.strftime("%Y-%m-%d")  # datetime/date object
        except AttributeError:
            return str(v or "").strip()

    def classify(headers):
        idx = {"name": None, "phone": None, "email": None, "last_visit": None}
        for i, h in enumerate(headers):
            hl = str(h or "").strip().lower()
            if idx["name"] is None and hl in name_keys: idx["name"] = i
            elif idx["phone"] is None and hl in phone_keys: idx["phone"] = i
            elif idx["email"] is None and hl in email_keys: idx["email"] = i
            elif idx["last_visit"] is None and hl in visit_keys: idx["last_visit"] = i
        return idx

    out = []
    if fn.endswith(".xlsx") or raw[:2] == b"PK":
        from openpyxl import load_workbook
        import io
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        grid = [[c for c in row] for row in ws.iter_rows(values_only=True)]
        if not grid:
            return []
        idx = classify(grid[0])
        # αν δεν αναγνωρίστηκε header, υπέθεσε στήλη0=όνομα, 1=τηλέφωνο, 2=email, 3=τελ.επίσκεψη
        has_header = any(v is not None for v in idx.values())
        start = 1 if has_header else 0
        if not has_header:
            idx = {"name": 0, "phone": 1, "email": 2, "last_visit": 3}
        for row in grid[start:]:
            def g(k):
                i = idx[k]
                return row[i] if (i is not None and i < len(row)) else ""
            out.append({"name": g("name"), "phone": g("phone"), "email": g("email"),
                        "last_visit": _vstr(g("last_visit"))})
    else:
        import csv, io
        text = None
        for enc in ("utf-8-sig", "utf-8", "cp1253"):
            try:
                text = raw.decode(enc); break
            except Exception:
                continue
        if text is None:
            text = raw.decode("utf-8", errors="ignore")
        reader = list(csv.reader(io.StringIO(text)))
        if not reader:
            return []
        idx = classify(reader[0])
        has_header = any(v is not None for v in idx.values())
        start = 1 if has_header else 0
        if not has_header:
            idx = {"name": 0, "phone": 1, "email": 2, "last_visit": 3}
        for row in reader[start:]:
            def g(k):
                i = idx[k]
                return row[i] if (i is not None and i < len(row)) else ""
            out.append({"name": g("name"), "phone": g("phone"), "email": g("email"),
                        "last_visit": _vstr(g("last_visit"))})
    return out


def register(app):
    app.register_blueprint(bp)
    print("[BACK_OFFICE] blueprint loaded — /<slug>/back-office")


_SETUP_HTML = r"""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Στήσιμο · {{ biz_name }}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f6f8fb;color:#0f172a;font-family:-apple-system,"Segoe UI",Roboto,Inter,sans-serif;font-size:14px;padding:28px 16px}
.wrap{max-width:620px;margin:0 auto}
h1{font-size:22px;margin-bottom:4px}
.sub{color:#64748b;font-size:14px;margin-bottom:22px}
.card{background:#fff;border:1px solid #e8edf5;border-radius:16px;padding:20px;margin-bottom:16px;box-shadow:0 1px 2px rgba(16,24,40,.04),0 4px 12px rgba(16,24,40,.05)}
.card h3{font-size:15px;margin-bottom:14px;font-weight:700}
label{display:block;font-size:12px;color:#475569;margin:10px 0 5px;font-weight:600}
input,select{width:100%;border:1px solid #e2e8f0;border-radius:9px;padding:10px 12px;font-size:14px;font-family:inherit}
input:focus,select:focus{outline:none;border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.12)}
.row{display:flex;gap:10px}.row>div{flex:1}
.days{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}
.day{display:flex;align-items:center;gap:5px;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:8px;padding:6px 10px;font-size:13px;cursor:pointer}
.svc{display:flex;gap:8px;align-items:center;margin-bottom:8px}
.svc input.n{flex:2}.svc input.p{flex:1}.svc input.d{width:80px}
.svc .x{background:#fef2f2;color:#dc2626;border:1px solid #fecaca;border-radius:7px;width:34px;height:38px;cursor:pointer;flex:0 0 auto}
.add{background:#eff6ff;color:#2563eb;border:1px solid #bfdbfe;border-radius:9px;padding:9px 14px;font-size:13px;font-weight:600;cursor:pointer;margin-top:4px}
.save{background:#2563eb;color:#fff;border:0;border-radius:11px;padding:14px;font-size:15px;font-weight:700;cursor:pointer;width:100%;margin-top:6px}
.save:disabled{opacity:.5}
.msg{text-align:center;margin-top:12px;font-size:14px}
.hint{font-size:12px;color:#94a3b8;margin-top:6px}
a{color:#2563eb}
</style></head><body><div class="wrap">
<h1>👋 Στήσε το ιατρείο σου</h1>
<div class="sub">2 λεπτά: ώρες + υπηρεσίες με τιμές. Μετά το AI κλείνει ραντεβού & εκδίδει σωστά τιμολόγια.</div>

<div class="card"><h3>🕐 Ωράριο</h3>
  <div class="row">
    <div><label>Άνοιγμα</label><input id="ds" type="time" value="09:00"/></div>
    <div><label>Κλείσιμο</label><input id="de" type="time" value="21:00"/></div>
    <div><label>Διάρκεια slot (λεπτά)</label><input id="slot" type="number" value="30" min="5" max="180"/></div>
  </div>
  <label>Κλειστά</label><div class="days" id="days"></div>
</div>

<div class="card"><h3>💉 Υπηρεσίες & τιμές</h3>
  <div id="svcs"></div>
  <button class="add" onclick="addSvc()">+ Προσθήκη υπηρεσίας</button>
  <div class="hint">Η τιμή χρησιμοποιείται για αυτόματα τιμολόγια. Άσε 0 αν δεν θες τιμή.</div>
</div>

<div class="card"><h3>🔗 Σύνδεση Google Calendar <span class="muted" style="font-weight:400;font-size:12px">(προαιρετικό, συνιστάται)</span></h3>
  <div class="hint" id="calhelp" style="margin-bottom:10px">Σύνδεσε το ημερολόγιό σου ώστε ο AI βοηθός να βλέπει το <b>πραγματικό σου πρόγραμμα</b> και να κλείνει ραντεβού κατευθείαν εκεί.</div>
  <label>Email ημερολογίου σου (Google Calendar ID)</label>
  <input id="calid" placeholder="π.χ. iatreio@gmail.com"/>
  <div class="hint" id="calshare"></div>
</div>

<div class="card"><h3>☀️ Ημερήσια ενημέρωση <span class="muted" style="font-weight:400;font-size:12px">(ο AI σε ενημερώνει μόνος του)</span></h3>
  <div class="hint" style="margin-bottom:10px">Κάθε πρωί ο AI σου στέλνει σύντομη σύνοψη: τι ραντεβού έχεις, πόσα ανέκτησε, ποιος χρωστάει, τι εκκρεμεί.</div>
  <label class="day" style="display:inline-flex"><input id="brief_on" type="checkbox" checked/> Ενεργή</label>
  <label style="margin-top:12px">Πώς να σε ειδοποιεί</label>
  <select id="brief_ch">
    <option value="auto">Αυτόματα (SMS αν υπάρχει, αλλιώς email)</option>
    <option value="email">Email</option>
    <option value="sms">SMS</option>
    <option value="voice">Φωνητική κλήση (διαβάζει τη σύνοψη)</option>
  </select>
  <div class="hint">Το email δουλεύει άμεσα. SMS/φωνή ενεργοποιούνται μόλις συνδεθεί αριθμός.</div>
</div>

<div class="card"><h3>🧑‍💼 Η γραμματέα σου <span class="muted" style="font-weight:400;font-size:12px">(εξατομίκευσε τη φωνή)</span></h3>
  <div class="hint" style="margin-bottom:10px">Δώσε στη γραμματέα σου όνομα, φωνή & ύφος. Αυτά μπαίνουν στην <b>τηλεφωνική AI</b> ώστε να ακούγεται σαν δική σου — όχι γενική.</div>
  <div class="row">
    <div><label>Όνομα γραμματέα</label><input id="v_name" placeholder="π.χ. Άννα"/></div>
    <div><label>Φωνή</label><select id="v_key">
      <option value="madlen">Γυναικεία — ζεστή (ιατρείο)</option>
      <option value="athina">Γυναικεία — νεανική</option>
      <option value="stefanos">Αντρική — επαγγελματική</option>
      <option value="dimitris">Αντρική — ώριμη</option>
    </select></div>
  </div>
  <label>Ύφος</label>
  <select id="v_tone">
    <option value="friendly">Φιλικό & ζεστό</option>
    <option value="professional">Επαγγελματικό</option>
    <option value="formal">Επίσημο (πληθυντικός ευγενείας)</option>
    <option value="casual">Χαλαρό & προσιτό</option>
    <option value="energetic">Δυναμικό & ενθουσιώδες</option>
  </select>
  <label>Χαιρετισμός (προαιρετικό — άσε κενό για αυτόματο)</label>
  <input id="v_greet" placeholder="π.χ. Καλημέρα, Φυσικοθεραπευτήριο Παπαδόπουλος, πώς να βοηθήσω;"/>
  <label>Ειδικές οδηγίες (προαιρετικό)</label>
  <input id="v_instr" placeholder="π.χ. ρώτα πάντα αν είναι πρώτη επίσκεψη · κλειστά τον Αύγουστο"/>
  <div class="hint">Οι ειδικές οδηγίες υπερισχύουν — π.χ. «μην κλείνεις ραντεβού Σάββατο απόγευμα».</div>
</div>

<div class="card">
  <h3>🎙️ Τηλεφωνική AI (φωνή)</h3>
  <div class="hint" style="margin-bottom:10px">Με <b>ένα κλικ</b> στήνεται AI που σηκώνει το τηλέφωνο, ξέρει τις υπηρεσίες & τις ώρες σου και κλείνει ραντεβού μόνη της. Κάνεις προώθηση (call-forward) το τηλέφωνο του ιατρείου εκεί. <i>Αποθήκευσε πρώτα τα στοιχεία.</i></div>
  <input id="sipnum" placeholder="Ελληνικός αριθμός +30… (production)" style="width:100%;padding:10px;border:1px solid #e8edf5;border-radius:8px;margin-bottom:8px"/>
  <div class="hint" style="margin-bottom:10px;font-size:12px">Βάλε ελληνικό DID → μηδέν διεθνής χρέωση σε γιατρό & ασθενείς. Άσ' το κενό = US νούμερο μόνο για test.</div>
  <button class="add" type="button" id="voicebtn" onclick="provisionVoice()">🎙️ Στήσε τη φωνή (1 κλικ)</button>
  <div class="msg" id="voicemsg"></div>
</div>

<button class="save" id="save" onclick="save()">Αποθήκευση & συνέχεια →</button>
<div class="msg" id="msg"></div>
<div class="hint" style="text-align:center;margin-top:14px">Έχεις παλιούς ασθενείς; Θα τους εισάγεις από Excel στο <a href="/{{ slug }}/back-office?t={{ t }}">back-office</a>.</div>

</div>
<script>
const SLUG="{{ slug }}", T="{{ t }}";
const $=s=>document.querySelector(s);
let DAYS=["Δευτέρα","Τρίτη","Τετάρτη","Πέμπτη","Παρασκευή","Σάββατο","Κυριακή"];
function svcRow(n,p,d){return `<div class="svc"><input class="n" placeholder="π.χ. Συνεδρία φυσικοθεραπείας" value="${(n||'').replace(/"/g,'&quot;')}"/><input class="p" type="number" placeholder="€" value="${p||''}"/><input class="d" type="number" placeholder="λεπτά" value="${d||''}"/><button class="x" onclick="this.parentElement.remove()">✕</button></div>`}
function addSvc(n,p,d){ $('#svcs').insertAdjacentHTML('beforeend', svcRow(n,p,d)); }
function renderDays(closed){ $('#days').innerHTML=DAYS.map((d,i)=>`<label class="day"><input type="checkbox" data-d="${i}" ${closed.includes(i)?'checked':''}/> ${d}</label>`).join(''); }
fetch(`/api/${SLUG}/setup-data?t=${T}`).then(r=>r.json()).then(d=>{
  if(d.days) DAYS=d.days;
  $('#ds').value=d.day_start||'09:00'; $('#de').value=d.day_end||'21:00'; $('#slot').value=d.slot_minutes||30;
  renderDays(d.closed_days||[6]);
  ($('#svcs').innerHTML='');
  if(d.services&&d.services.length) d.services.forEach(s=>addSvc(s.name,s.price,s.duration));
  else addSvc('',''),addSvc('','');
  if(d.google_calendar_id) $('#calid').value=d.google_calendar_id;
  $('#brief_on').checked = (d.briefing_enabled!==0);
  if(d.briefing_channel) $('#brief_ch').value=d.briefing_channel;
  // εξατομίκευση φωνής
  if($('#v_name')) $('#v_name').value=d.voice_assistant_name||'Άννα';
  if($('#v_key')&&d.voice_key) $('#v_key').value=d.voice_key;
  if($('#v_tone')&&d.voice_tone) $('#v_tone').value=d.voice_tone;
  if($('#v_greet')) $('#v_greet').value=d.voice_greeting||'';
  if($('#v_instr')) $('#v_instr').value=d.voice_custom_instructions||'';
  if(d.calendar_auth_mode==='oauth') $('#calshare').innerHTML='✅ Συνδεδεμένο μέσω <b>Google login (OAuth)</b> — δεν χρειάζεται sharing. Βάλε το Calendar ID σου (ή άσε <b>primary</b> για το κύριο ημερολόγιο).';
  else if(d.calendar_share_email) $('#calshare').innerHTML='📌 Για να συνδεθεί: άνοιξε Google Calendar → Settings → «Share with specific people» → πρόσθεσε <b>'+d.calendar_share_email+'</b> με δικαίωμα «Make changes to events».';
  else $('#calshare').innerHTML='<span style="color:#b45309">⚠️ Το Google Calendar δεν είναι ρυθμισμένο ακόμα στον server (OAuth ή service-account).</span>';
}).catch(()=>{ renderDays([6]); addSvc(); addSvc(); });
async function save(){
  const closed=[...document.querySelectorAll('#days input:checked')].map(c=>+c.dataset.d);
  const services=[...document.querySelectorAll('.svc')].map(r=>({name:r.querySelector('.n').value.trim(),price:r.querySelector('.p').value,duration:r.querySelector('.d').value})).filter(s=>s.name);
  if(!services.length){ $('#msg').innerHTML='<span style="color:#dc2626">Πρόσθεσε τουλάχιστον μία υπηρεσία.</span>'; return; }
  const b={day_start:$('#ds').value,day_end:$('#de').value,slot_minutes:$('#slot').value,closed_days:closed,services,google_calendar_id:($('#calid').value||'').trim(),briefing_enabled:$('#brief_on').checked,briefing_channel:$('#brief_ch').value,
    voice_assistant_name:($('#v_name')&&$('#v_name').value||'').trim(),
    voice_key:($('#v_key')&&$('#v_key').value||''),
    voice_tone:($('#v_tone')&&$('#v_tone').value||''),
    voice_greeting:($('#v_greet')&&$('#v_greet').value||'').trim(),
    voice_custom_instructions:($('#v_instr')&&$('#v_instr').value||'').trim()};
  $('#save').disabled=true; $('#msg').textContent='Αποθήκευση...';
  try{
    const r=await fetch(`/api/${SLUG}/setup?t=${T}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(x=>x.json());
    if(r.ok){ $('#msg').innerHTML='<span style="color:#059669">✓ '+r.msg+' Πάμε στο back-office…</span>'; setTimeout(()=>location.href=`/${SLUG}/back-office?t=${T}`,900); }
    else { $('#msg').innerHTML='<span style="color:#dc2626">⚠️ '+(r.error||'Σφάλμα')+'</span>'; $('#save').disabled=false; }
  }catch(e){ $('#msg').innerHTML='<span style="color:#dc2626">Σφάλμα δικτύου</span>'; $('#save').disabled=false; }
}
async function provisionVoice(){
  $('#voicebtn').disabled=true; $('#voicemsg').textContent='Στήνω τη φωνή… (≈1 λεπτό)';
  const sip=($('#sipnum')&&$('#sipnum').value||'').trim();
  try{
    const r=await fetch(`/api/${SLUG}/provision-voice?t=${T}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sip_number:sip})}).then(x=>x.json());
    if(r.ok){ $('#voicemsg').innerHTML='<span style="color:#059669">✓ '+r.msg+'</span>'; }
    else { $('#voicemsg').innerHTML='<span style="color:#b45309">⚠️ '+(r.error||'Σφάλμα')+'</span>'; $('#voicebtn').disabled=false; }
  }catch(e){ $('#voicemsg').innerHTML='<span style="color:#dc2626">Σφάλμα δικτύου</span>'; $('#voicebtn').disabled=false; }
}
</script></body></html>"""


_BO_HTML = r"""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Back-Office · {{ biz_name }}</title>
<link rel="manifest" href="/{{ slug }}/app-manifest.json?t={{ t }}"/>
<meta name="theme-color" content="#6E56CF"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-title" content="{{ biz_name }}"/>
<link rel="apple-touch-icon" href="/icons/icon-180.png"/>
<style>
:root{--bg:#f6f8fb;--card:#ffffff;--card2:#f8fafc;--line:#e8edf5;--txt:#0f172a;--mut:#94a3b8;--sec:#475569;--acc:#2563eb;--acc2:#1d4ed8;--c2:#2563eb;--c3:#2563eb;--ok:#059669;--warn:#b45309;--bad:#dc2626}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,"Segoe UI",Roboto,Inter,sans-serif;font-size:14px;min-height:100vh}
.neon{color:#2563eb;font-weight:700}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap;position:sticky;top:0;z-index:5;background:#ffffff}
header h1{font-size:17px;font-weight:700}
header .sub{color:var(--sec);font-size:12px}
.badge{background:#ecfdf5;color:var(--ok);padding:3px 10px;border-radius:20px;font-size:11px;border:1px solid #a7f3d0}
.navlink{color:var(--acc);text-decoration:none;font-size:12px;border:1px solid var(--line);padding:5px 11px;border-radius:8px;transition:.2s}
.navlink:hover{border-color:var(--acc);background:#f8fafc}
nav{display:flex;gap:4px;padding:10px 24px;border-bottom:1px solid var(--line);overflow-x:auto;flex-wrap:wrap;background:#ffffff}
nav button{background:transparent;color:var(--sec);border:0;padding:8px 14px;border-radius:10px;cursor:pointer;font-size:13px;white-space:nowrap;transition:.2s}
nav button.on{background:#eff6ff;color:var(--acc);border:1px solid #bfdbfe;font-weight:600}
main{padding:20px 24px;max-width:1200px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:18px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;box-shadow:0 1px 2px rgba(16,24,40,.04),0 4px 12px rgba(16,24,40,.05);transition:.2s}
.kpi:hover{transform:translateY(-2px);border-color:#cbd5e1}
.kpi .n{font-size:24px;font-weight:700;color:var(--txt)}
.kpi .l{color:var(--sec);font-size:12px;margin-top:4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:14px;box-shadow:0 1px 2px rgba(16,24,40,.04),0 4px 12px rgba(16,24,40,.05)}
.card h3{font-size:14px;margin-bottom:12px;display:flex;align-items:center;gap:8px;font-weight:700;color:var(--txt)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #eef2f7}
th{color:#64748b;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.4px}
tr:hover td{background:#f8fafc}
.pill{padding:2px 9px;border-radius:20px;font-size:11px;display:inline-block;font-weight:600}
.pill.ok{background:#ecfdf5;color:#059669}
.pill.warn{background:#fffbeb;color:#b45309}
.pill.bad{background:#fef2f2;color:#dc2626}
.pill.mut{background:#f1f5f9;color:#64748b}
.link{color:var(--acc);cursor:pointer;text-decoration:none;font-weight:500}
.abtn{background:var(--acc);color:#fff;border:0;padding:9px 14px;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;transition:.2s}
.abtn:hover{background:var(--acc2)}
.abtn:disabled{opacity:.5;cursor:wait}
.miniact{background:#ecfdf5;color:var(--ok);border:1px solid #a7f3d0;padding:4px 10px;border-radius:7px;font-size:12px;cursor:pointer;font-weight:600}
.miniact:hover{background:#d1fae5}
#modal textarea,#modal input{width:100%;background:#ffffff;border:1px solid var(--line);color:var(--txt);padding:11px 13px;border-radius:10px;font-size:14px;margin-bottom:4px;font-family:inherit}
#modal textarea:focus,#modal input:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 3px rgba(37,99,235,.12)}
#modal textarea{min-height:80px;resize:vertical}
#modal label{display:block;font-size:12px;color:var(--sec);margin:10px 0 5px}
.agent{display:flex;gap:12px;padding:14px;border:1px solid var(--line);border-radius:12px;margin-bottom:10px;background:#f8fafc}
.agent .dot{width:10px;height:10px;border-radius:50%;background:var(--ok);margin-top:4px;flex:0 0 auto}
.agent .nm{font-weight:600;margin-bottom:3px}
.agent .ro{color:var(--sec);font-size:12px;line-height:1.5}
.agent .meta{color:var(--acc);font-size:11px;margin-top:6px}
.muted{color:var(--mut)}
.right{text-align:right}
#modal{position:fixed;inset:0;background:rgba(15,23,42,.4);display:none;align-items:flex-start;justify-content:center;padding:40px 16px;overflow:auto;z-index:50}
#modal .box{background:var(--card);border:1px solid var(--line);border-radius:16px;max-width:760px;width:100%;padding:22px;box-shadow:0 10px 40px rgba(16,24,40,.15)}
.close{float:right;cursor:pointer;color:var(--mut);font-size:20px}
.tl{border-left:2px solid var(--line);padding-left:14px;margin-left:6px}
.tl .ev{margin-bottom:12px}
.tl .ev .h{font-size:12px;color:var(--mut)}
small.tag{background:#f1f5f9;border:1px solid var(--line);padding:2px 7px;border-radius:6px;color:#64748b;font-size:10px;margin-right:4px}
</style></head><body>
<header>
  <h1>🧠 <span class="neon">Back-Office</span></h1><span class="sub">{{ biz_name }}</span>
  <span class="badge" id="agentsBadge">agents…</span>
  <button class="navlink" onclick="guide()" style="margin-left:auto;cursor:pointer;background:none">❓ Πώς λειτουργεί</button>
  <a class="navlink" href="/{{ slug }}" target="_blank">📞 Front-desk</a>
  <a class="navlink" href="/{{ slug }}/dashboard?t={{ t }}" target="_blank">📊 Έσοδα</a>
  <button class="navlink" onclick="loadSample()" style="cursor:pointer;background:none" title="Δες πώς δουλεύει με δείγμα δεδομένων">🧪 Δείγμα</button>
  <button class="navlink" onclick="resetData()" style="cursor:pointer;background:none" title="Καθάρισε όλα τα δεδομένα">🗑️ Καθάρισε</button>
  <button class="navlink" id="installbtn" onclick="installApp()" style="cursor:pointer;background:none;display:none" title="Εγκατάσταση σαν app">📲 Εγκατάσταση app</button>
  <button class="navlink" onclick="manageSub()" style="cursor:pointer;background:none">💳 Συνδρομή</button>
  <span class="sub">🔒 Κρυπτογραφημένα</span>
</header>
<nav id="tabs">
  <button class="on" data-t="day">📅 Ημέρα</button>
  <button data-t="overview">📊 Σύνοψη</button>
  <button data-t="autos">🤖 Αυτόματα</button>
  <button data-t="patients">👥 Φάκελοι</button>
  <button data-t="finance">💶 Οικονομικά</button>
  <button data-t="esodaView">📈 Έσοδα</button>
  <button data-t="inventory">📦 Αποθήκη</button>
  <button data-t="setupView">⚙️ Ρυθμίσεις</button>
  <button data-t="assistant">💬 Βοηθός</button>
</nav>
<main id="view"><p class="muted">Φόρτωση…</p></main>
<div id="modal"><div class="box"><span class="close" onclick="closeM()">×</span><div id="mbody"></div></div></div>
<script>
const SLUG="{{ slug }}", T="{{ t }}";
const $=s=>document.querySelector(s);
const api=p=>fetch(`/api/${SLUG}/back-office/${p}${p.includes('?')?'&':'?'}t=${T}`).then(r=>r.json());
const eur=n=>'€'+(Math.round((n||0)*100)/100).toLocaleString('el-GR');
const post=(p,b)=>fetch(`/api/${SLUG}/back-office/${p}?t=${T}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
async function loadSample(){
  if(!confirm('Φόρτωση δείγματος δεδομένων για να δεις πώς δουλεύει το back office + ο Βοηθός;')) return;
  const r=await fetch(`/api/${SLUG}/load-sample?t=${T}`,{method:'POST'}).then(x=>x.json()).catch(()=>({error:'σφάλμα δικτύου'}));
  alert(r.ok?('✓ '+r.msg):('⚠️ '+(r.error||'Σφάλμα')));
  if(r.ok) location.reload();
}
async function resetData(){
  if(!confirm('Σίγουρα; Θα διαγραφούν ΟΛΑ τα δεδομένα (ασθενείς, ραντεβού, τιμολόγια). Κρατιούνται μόνο υπηρεσίες/ώρες.')) return;
  const r=await fetch(`/api/${SLUG}/reset-data?t=${T}`,{method:'POST'}).then(x=>x.json()).catch(()=>({error:'σφάλμα δικτύου'}));
  alert(r.ok?('✓ '+r.msg):('⚠️ '+(r.error||'Σφάλμα')));
  if(r.ok) location.reload();
}
let _deferredPrompt=null;
window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();_deferredPrompt=e;const b=document.getElementById('installbtn');if(b)b.style.display='';});
async function installApp(){
  if(!_deferredPrompt){alert('Το app είναι ήδη εγκατεστημένο, ή πάτα «Προσθήκη στην αρχική οθόνη» από το μενού του browser.');return;}
  _deferredPrompt.prompt(); await _deferredPrompt.userChoice; _deferredPrompt=null;
  const b=document.getElementById('installbtn'); if(b)b.style.display='none';
}
if('serviceWorker' in navigator){ navigator.serviceWorker.register('/sw.js').catch(()=>{}); }
let DATA={};
// Real-time: η οθόνη «Ημέρα» ανανεώνεται μόνη της ώστε οι τηλεφωνικές κρατήσεις
// του AI βοηθού να εμφανίζονται ζωντανά (ο γιατρός βλέπει τι έκλεισε η φωνή).
let _dayPoll=null;
function stopDayPoll(){ if(_dayPoll){ clearInterval(_dayPoll); _dayPoll=null; } }

function actionForm(title, fields, onSubmit){
  const inputs=fields.map(f=> f.type==='textarea'
    ? `<label>${f.label}</label><textarea id="af_${f.name}"></textarea>`
    : `<label>${f.label}</label><input id="af_${f.name}" type="${f.type||'text'}" ${f.value?`value="${f.value}"`:''}/>`).join('');
  $('#mbody').innerHTML=`<h2 style="margin-bottom:6px">${title}</h2>${inputs}
    <button class="abtn" id="af_go" style="margin-top:14px;width:100%">Αποθήκευση</button>
    <div class="muted" id="af_msg" style="margin-top:10px;text-align:center"></div>`;
  $('#modal').style.display='flex';
  $('#af_go').onclick=async()=>{
    const v={}; fields.forEach(f=>v[f.name]=document.getElementById('af_'+f.name).value.trim());
    $('#af_go').disabled=true; $('#af_msg').textContent='Αποθήκευση...';
    let r; try{ r=await onSubmit(v); }catch(e){ r={error:'σφάλμα δικτύου'}; }
    if(r&&r.ok){ $('#af_msg').innerHTML='<span style="color:var(--ok)">✓ Αποθηκεύτηκε!</span>'; setTimeout(closeM,650); }
    else { $('#af_msg').innerHTML='<span style="color:var(--bad)">⚠️ '+((r&&r.error)||'Σφάλμα')+'</span>'; $('#af_go').disabled=false; }
  };
}
function newPatient(){ actionForm('➕ Νέος Ασθενής',[
  {name:'name',label:'Ονοματεπώνυμο'},{name:'phone',label:'Τηλέφωνο'},
  {name:'email',label:'Email (προαιρετικό)'},{name:'medical_alerts',label:'Ιατρικές ειδοποιήσεις — αλλεργίες/αγωγή (κρυπτογραφείται)',type:'textarea'}
], v=>post('patient',v).then(r=>{ if(r.ok) patients(); return r; })); }
function addNote(ph){ actionForm('📝 Νέα Σημείωση Φακέλου',[
  {name:'title',label:'Τίτλος'},{name:'body',label:'Σημείωση (κρυπτογραφείται)',type:'textarea'}
], v=>post('note',{title:v.title,body:v.body,phone:ph}).then(r=>{ if(r.ok) openPatient(ph); return r; })); }
function addInvoice(ph,name){ actionForm('🧾 Νέο Τιμολόγιο',[
  {name:'service',label:'Υπηρεσία'},{name:'amount',label:'Ποσό σε € (με ΦΠΑ αν ισχύει)',type:'number'}
], v=>post('invoice',{service:v.service,amount:v.amount,phone:ph,name:name}).then(r=>{ if(r.ok) openPatient(ph); return r; })); }
async function payInv(id){ const r=await post('payment',{invoice_id:id}); if(r.ok) finance(); else alert(r.error||'Σφάλμα'); }
async function taskDone(id){ const r=await post('task-done',{task_id:id}); if(r.ok) autos(); else alert(r.error||'Σφάλμα'); }

function pill(s){const m={paid:'ok',unpaid:'warn',partial:'warn',sent:'ok',pending:'mut',active:'ok',high:'bad',normal:'mut'};return `<span class="pill ${m[s]||'mut'}">${s}</span>`}

async function day(dateStr){
  $('#view').innerHTML='<p class="muted">Φόρτωση…</p>';
  const d=await api(dateStr?('day?date='+dateStr):'day').catch(()=>({}));
  if(!d||!d.till){ $('#view').innerHTML='<p class="muted">Σφάλμα φόρτωσης.</p>'; return; }
  DATA.day=d;
  const t=d.till, c=d.counts, tm=d.tomorrow;
  const sp=s=>({completed:'<span class="pill ok">ήρθε</span>',no_show:'<span class="pill bad">δεν ήρθε</span>',pending_outcome:'<span class="pill warn">εκκρεμεί</span>',confirmed:'<span class="pill mut">προγραμματισμένο</span>'}[s]||pill(s));
  const vb='<span title="Κλείστηκε τηλεφωνικά από τον AI βοηθό" style="display:inline-block;background:#eef2ff;color:#4f46e5;border:1px solid #c7d2fe;border-radius:6px;font-size:10px;font-weight:700;padding:1px 6px;margin-left:6px;vertical-align:middle">📞 τηλ.</span>';
  const rows=d.appointments.length? d.appointments.map(a=>`<tr${a.fresh?' style="background:#eef2ff"':''}>
      <td><b>${a.time}</b></td>
      <td>${a.name||'(χωρίς όνομα)'}${a.source==='voice'?vb:''}<div class="muted" style="font-size:11px">${a.phone||''}</div></td>
      <td>${a.service||''}${a.price>0?` <span class="muted">· ${eur(a.price)}</span>`:''}</td>
      <td>${sp(a.status)}</td>
      <td class="right">${a.resolved?'<span class="muted">—</span>':`<button class="miniact" onclick="setOutcome(${a.id},'completed',this)">✓ Ήρθε</button> <button class="miniact" style="background:#fef2f2;color:#dc2626;border-color:#fecaca" onclick="setOutcome(${a.id},'no_show',this)">✕ Όχι</button>`}</td>
    </tr>`).join('') : '<tr><td colspan=5 class="muted">Κανένα ραντεβού αυτή τη μέρα.</td></tr>';
  const vfeed=(d.voice_feed&&d.voice_feed.length)?`<div class="card" id="vfeed" style="background:linear-gradient(135deg,#eef2ff,#fff);border-color:#c7d2fe">
      <div style="font-size:13px;color:#4f46e5;font-weight:700">📞 Ο AI βοηθός μόλις έκλεισε ${d.voice_feed.length>1?(d.voice_feed.length+' ραντεβού'):'ραντεβού'}</div>
      ${d.voice_feed.map(v=>`<div style="font-size:13px;margin-top:3px">✅ <b>${v.name||'πελάτης'}</b> · ${v.time}${v.service?(' · '+v.service):''}</div>`).join('')}
    </div>`:'';
  $('#view').innerHTML=`
  <div class="grid">
    <div class="kpi"><div class="n">${c.total}</div><div class="l">Ραντεβού ${d.is_today?'σήμερα':''}</div></div>
    <div class="kpi"><div class="n" style="color:var(--ok)">${c.done}</div><div class="l">Ήρθαν</div></div>
    <div class="kpi"><div class="n" style="color:var(--bad)">${c.no_show}</div><div class="l">Δεν ήρθαν</div></div>
    <div class="kpi" style="background:linear-gradient(135deg,#ecfdf5,#d1fae5);border-color:#a7f3d0"><div class="n" style="color:#047857">${eur(t.collected)}</div><div class="l" style="color:#065f46">💶 Ταμείο ${d.is_today?'σήμερα':''}</div></div>
  </div>
  ${vfeed}
  <div class="card" id="dayboard">
    <h3>📅 Πρόγραμμα — ${d.is_today?'Σήμερα':d.date}${c.voice?` <span class="muted" style="font-weight:400;font-size:13px">· ${c.voice} τηλεφωνικά 📞</span>`:''}
      <span style="margin-left:auto;display:flex;gap:6px">
        <button class="navlink" style="cursor:pointer;background:none" onclick="day('${d.prev}')">←</button>
        ${d.is_today?'':`<button class="navlink" style="cursor:pointer;background:none" onclick="day()">Σήμερα</button>`}
        <button class="navlink" style="cursor:pointer;background:none" onclick="day('${d.next}')">→</button>
      </span>
    </h3>
    <table><thead><tr><th>Ώρα</th><th>Ασθενής</th><th>Υπηρεσία</th><th>Κατάσταση</th><th></th></tr></thead><tbody>${rows}</tbody></table>
    ${t.expected>t.collected+0.01?`<p class="muted" style="margin-top:8px;font-size:12px">Αναμενόμενο ταμείο: ${eur(t.expected)} · Εισπράχθηκαν ως τώρα: ${eur(t.collected)}</p>`:''}
  </div>
  <div class="card" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <div><b>📨 Αυριανά ραντεβού</b> <span class="muted">(${tm.date})</span><div class="muted" style="font-size:12px">${tm.total} συνολικά · ${tm.to_confirm} χωρίς επιβεβαίωση</div></div>
    <button class="abtn" style="margin-left:auto" ${tm.to_confirm?'':'disabled'} onclick="confirmTomorrow(this)">Επιβεβαίωσε τα αυριανά${tm.to_confirm?` (${tm.to_confirm})`:''}</button>
  </div>
  <div class="card"><h3>⏳ Λίστα αναμονής <span style="margin-left:auto"><button class="abtn" onclick="addWait()">+ Προσθήκη</button></span></h3><div id="waitbox" class="muted">…</div></div>`;
  loadWaitlist();
  // live refresh ΜΟΝΟ στη σημερινή οθόνη (οι τηλεφωνικές κρατήσεις εμφανίζονται μόνες τους)
  stopDayPoll();
  if(d.is_today){
    _dayPoll=setInterval(()=>{
      if(document.visibilityState!=='visible') return;       // μην τρέχει σε κρυμμένη καρτέλα
      if(!document.getElementById('dayboard')){ stopDayPoll(); return; }  // ο γιατρός άλλαξε tab
      day();
    }, 20000);
  }
}
async function setOutcome(id,outcome,btn){
  if(btn) btn.disabled=true;
  const r=await post('booking-outcome',{booking_id:id,outcome}).catch(()=>({error:'σφάλμα δικτύου'}));
  if(!r.ok){ alert('⚠️ '+(r.error||'Σφάλμα')); if(btn)btn.disabled=false; return; }
  day(DATA.day&&!DATA.day.is_today?DATA.day.date:undefined);
}
async function confirmTomorrow(btn){
  if(btn){btn.disabled=true;btn.textContent='Στέλνω…';}
  const r=await post('confirm-tomorrow',{}).catch(()=>({error:'σφάλμα δικτύου'}));
  if(btn){btn.disabled=false;btn.textContent='Επιβεβαίωσε τα αυριανά';}  // restore πριν το refresh
  alert(r.ok?('✓ '+r.msg):('⚠️ '+(r.error||'Σφάλμα')));
  day(DATA.day&&!DATA.day.is_today?DATA.day.date:undefined);
}
async function loadWaitlist(){
  const box=$('#waitbox'); if(!box) return;
  const d=await api('waitlist').catch(()=>({waitlist:[]}));
  const w=d.waitlist||[];
  box.innerHTML = w.length ? '<table><tbody>'+w.map(x=>`<tr><td>${x.name||'(χωρίς όνομα)'}<div class="muted" style="font-size:11px">${x.phone}</div></td><td class="muted">${x.service||''}${x.note?(' · '+x.note):''}</td><td class="right"><button class="miniact" onclick="notifyWait(${x.id})">📨 Άνοιξε θέση</button> <button class="miniact" style="background:#f1f5f9;color:#64748b;border-color:#e2e8f0" onclick="removeWait(${x.id})">✕</button></td></tr>`).join('')+'</tbody></table>' : '<span class="muted">Κανείς στη λίστα αναμονής. Πρόσθεσε όποιον θέλει νωρίτερο ραντεβού — όταν ακυρωθεί κάποιο, τον ειδοποιείς με ένα κλικ.</span>';
}
function addWait(){ actionForm('⏳ Προσθήκη στη λίστα αναμονής',[
  {name:'name',label:'Ονοματεπώνυμο'},{name:'phone',label:'Τηλέφωνο',type:'tel'},
  {name:'service',label:'Υπηρεσία (προαιρετικό)'},{name:'note',label:'Σημείωση — π.χ. προτιμά πρωί (προαιρετικό)'}
], v=>post('waitlist',v).then(r=>{ if(r.ok) loadWaitlist(); return r; })); }
async function notifyWait(id){ const r=await post('waitlist-notify',{id}); alert(r.ok?('✓ '+r.msg):('⚠️ '+(r.error||'Σφάλμα'))); loadWaitlist(); }
async function removeWait(id){ if(!confirm('Αφαίρεση από τη λίστα αναμονής;'))return; await post('waitlist-remove',{id}); loadWaitlist(); }

async function autos(){
  $('#view').innerHTML='<p class="muted">Φόρτωση…</p>';
  const d=await api('automations').catch(()=>({}));
  if(!d||!d.items){ $('#view').innerHTML='<p class="muted">Σφάλμα φόρτωσης.</p>'; return; }
  const rec=(d.recovered_eur>0)?`<div class="card" style="background:linear-gradient(135deg,#ecfdf5,#fff);border-color:#a7f3d0">
      <div style="font-size:13px;color:#059669;font-weight:700">💰 Ο βοηθός σού γύρισε πίσω (${d.days} μέρες)</div>
      <div style="font-size:32px;font-weight:800;color:#047857">${eur(d.recovered_eur)}</div>
      <div class="muted" style="font-size:12px">${d.recovered_count} ασθενείς που είχες χάσει, επέστρεψαν — αυτόματα</div></div>`:'';
  const feed=d.items.length?`<div class="card">
      <h3>🤖 Τι έκανε ο βοηθός σου μόνος του <span class="muted" style="font-weight:400">(${d.days} μέρες)</span> <span style="margin-left:auto;font-size:20px;font-weight:800;color:var(--acc)">${d.total_actions}</span></h3>
      <table><tbody>${d.items.map(i=>`<tr><td style="font-size:18px;width:36px">${i.icon}</td><td class="right" style="width:50px"><b>${i.n}</b></td><td>${i.label}</td></tr>`).join('')}</tbody></table>
      <p class="muted" style="font-size:12px;margin-top:8px">⚡ Όλα έγιναν χωρίς να κάνεις τίποτα — ο βοηθός δουλεύει 24/7.</p></div>`
    :`<div class="card"><h3>🤖 Ο βοηθός σου δουλεύει 24/7</h3><p class="muted">Μόλις μπουν ραντεβού & ασθενείς, εδώ θα βλέπεις ΤΙ κάνει αυτόματα για σένα: υπενθυμίσεις, win-back χαμένων, recovery από no-show, τιμολόγια. Δεν χρειάζεται να κάνεις τίποτα.</p></div>`;
  const acts=(d.actions&&d.actions.length)?`<div class="card"><h3>⚡ Ή κάνε το τώρα με ένα κλικ</h3>${d.actions.map(a=>`
      <div style="border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:8px">
        <div style="display:flex;align-items:center"><b>${a.icon} ${a.title}</b><span style="margin-left:auto;font-weight:800;color:#047857">≈${eur(a.eur)}</span></div>
        <div class="muted" style="font-size:12px;margin:4px 0 8px">${a.subtitle}</div>
        <button class="abtn" style="width:100%" onclick="runAction('${a.key}',this)">${a.cta} →</button></div>`).join('')}</div>`:'';
  $('#view').innerHTML=rec+feed+acts+`<div id="autoTasks"></div>`;
  const t=await api('tasks').catch(()=>({tasks:[]}));
  if(t.tasks&&t.tasks.length){
    $('#autoTasks').innerHTML=`<div class="card"><h3>📋 Χρειάζονται εσένα (${t.tasks.length})</h3>
      <table><tbody>${t.tasks.slice(0,12).map(x=>`<tr><td>${pill(x.priority)}</td><td>${x.title}</td><td class="right"><button class="miniact" onclick="taskDone(${x.id})">✓ Έγινε</button></td></tr>`).join('')}</tbody></table></div>`;
  }
}

async function today(){
  $('#view').innerHTML='<p class="muted">Φόρτωση…</p>';
  const d=await api('today').catch(()=>({}));
  const rec = (d.recovered_30d_eur>0) ? `<div class="card" style="background:linear-gradient(135deg,#ecfdf5,#fff);border-color:#a7f3d0">
      <div style="font-size:13px;color:#059669;font-weight:700">✅ Σου έχουμε γυρίσει πίσω (30 μέρες)</div>
      <div style="font-size:30px;font-weight:800;color:#047857">${eur(d.recovered_30d_eur)}</div>
      <div class="muted" style="font-size:12px">${d.recovered_30d_count} ασθενείς που είχες χάσει, επέστρεψαν</div></div>` : '';
  if(!d.actions || !d.actions.length){
    $('#view').innerHTML=rec+`<div class="card" style="text-align:center;padding:34px">
      <div style="font-size:40px">🎉</div><h3 style="justify-content:center">Όλα τακτοποιημένα!</h3>
      <p class="muted">Δεν υπάρχουν χαμένοι ασθενείς ή χρωστούμενα τώρα. Μόλις εμφανιστούν, θα τα δεις εδώ με ένα κουμπί.</p></div>`;
    return;
  }
  const cards=d.actions.map(a=>`
    <div class="card">
      <h3>${a.icon} ${a.title} <span style="margin-left:auto;font-size:19px;font-weight:800;color:#047857">≈${eur(a.eur)}</span></h3>
      <div class="muted" style="margin:-6px 0 10px">${a.subtitle}</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px">${a.who.map(w=>`<span class="pill mut">${w}</span>`).join('')}${a.count>a.who.length?`<span class="pill mut">+${a.count-a.who.length} ακόμα</span>`:''}</div>
      <button class="abtn" style="width:100%" onclick="runAction('${a.key}',this)">${a.cta} →</button>
    </div>`).join('');
  $('#view').innerHTML=`
    <div class="card" style="background:linear-gradient(135deg,#eef2ff,#fff);border-color:#c7d2fe">
      <div style="font-size:13px;color:#4f46e5;font-weight:700">💰 ΣΗΜΕΡΑ μπορείς να φέρεις πίσω</div>
      <div style="font-size:34px;font-weight:800;color:#4338ca">${eur(d.total_at_risk_eur)}</div>
      <div class="muted" style="font-size:12px">${d.actions.length} κινήσεις — πάτα ένα κουμπί κι ο βοηθός τις κάνει για σένα.</div>
    </div>
    ${rec}${cards}`;
}
async function runAction(key,btn){
  const orig = btn ? btn.textContent : '';
  if(btn){btn.disabled=true;btn.textContent='Γίνεται…';}
  const r=await post('run-action',{key}).catch(()=>({error:'σφάλμα δικτύου'}));
  alert(r.ok?('✓ '+r.msg):('⚠️ '+(r.error||'Σφάλμα')));
  if(btn){btn.disabled=false;btn.textContent=orig;}
  autos();
}

async function overview(){
  const s=await api('summary');DATA.sum=s;const f=s.finance,cal=s.calendar_today;
  $('#view').innerHTML=`
  <div class="grid">
    <div class="kpi"><div class="n">${s.patients}</div><div class="l">Φάκελοι ασθενών</div></div>
    <div class="kpi" style="background:linear-gradient(135deg,#ecfdf5,#d1fae5);border-color:#a7f3d0"><div class="n" style="color:#047857">${eur(f.revenue)}</div><div class="l" style="color:#065f46">Έσοδα (30 ημ.)</div></div>
    <div class="kpi"><div class="n" style="color:var(--warn)">${eur(f.outstanding)}</div><div class="l">Ανοιχτά υπόλοιπα</div></div>
    <div class="kpi"><div class="n" style="color:#059669">${eur(f.profit)}</div><div class="l">Κέρδος (30 ημ.)</div></div>
    <div class="kpi"><div class="n">${cal.utilization_pct}%</div><div class="l">Πληρότητα σήμερα</div></div>
    <div class="kpi"><div class="n" style="color:var(--acc)">${s.graph_links}</div><div class="l">Διασυνδέσεις (graph)</div></div>
  </div>
  <div id="ovChannels" class="muted" style="margin:2px 0 12px;font-size:13px">Κανάλια ειδοποιήσεων…</div>
  <div class="card"><h3>📋 Προτεραιότητες σήμερα <span class="muted" style="font-weight:400">(από τους agents)</span></h3><div id="ovTasks" class="muted">…</div></div>
  <div class="card"><h3>⚠️ Ραντεβού σε ρίσκο <span class="muted" style="font-weight:400">(επόμενες 7 μέρες — με τον λόγο)</span></h3><div id="ovRisk" class="muted">Υπολογισμός…</div></div>
  <div class="card"><h3>💤 Χαμένοι ασθενείς — recall <span class="muted" style="font-weight:400">(ποιος έπρεπε να έχει ξανάρθει)</span></h3><div id="ovRecall" class="muted">Υπολογισμός…</div></div>
  <div class="card"><h3>💶 Τι αφήνει λεφτά <span class="muted" style="font-weight:400">(90 ημέρες, ανά υπηρεσία)</span></h3><div id="ovRevAn" class="muted">Υπολογισμός…</div></div>
  <div class="card"><h3>📈 Πρόβλεψη ζήτησης <span class="muted" style="font-weight:400">(επόμενες 14 μέρες)</span></h3><div id="ovForecast" class="muted">Υπολογισμός…</div></div>
  <div class="card"><h3>💶 Οικονομικά</h3><p class="muted">${f.mydata_pending} τιμολόγια έτοιμα για τον λογιστή σου · Έσοδα τιμολογημένα: ${eur(f.billed)} · Έξοδα: ${eur(f.expenses)}</p></div>`;
  const t=await api('tasks');
  $('#ovTasks').innerHTML = t.tasks.length? '<table><tbody>'+t.tasks.slice(0,8).map(x=>`<tr><td>${pill(x.priority)}</td><td>${x.title}</td><td class="muted">${x.kind}</td></tr>`).join('')+'</tbody></table>' : '<span class="muted">Όλα τακτοποιημένα ✓</span>';
  api('recall').then(rc=>{
    const el=$('#ovRecall'); if(!el) return;
    if(!(rc.candidates||[]).length){ el.innerHTML='<span class="muted">Κανένας χαμένος ασθενής ✓</span>'; return; }
    el.innerHTML=(rc.eur_potential?`<p style="margin:0 0 8px"><b style="color:#b45309">€${rc.eur_potential}</b> <span class="muted">δυναμικό από ${rc.total} ασθενείς που δεν ξαναήρθαν</span></p>`:'')+
      '<table><tbody>'+rc.candidates.slice(0,6).map(x=>
      `<tr><td><b>${x.name}</b></td><td class="muted">${x.reason}</td><td>${x.est_eur?('€'+x.est_eur):''}</td><td><a href="tel:${x.phone}" style="text-decoration:none">📞</a></td></tr>`).join('')+'</tbody></table>';
  }).catch(()=>{});
  api('revenue-analytics').then(ra=>{
    const el=$('#ovRevAn'); if(!el) return;
    if(!(ra.by_service||[]).length){ el.innerHTML='<span class="muted">'+(ra.insight||'Χωρίς τιμολόγια ακόμα.')+'</span>'; return; }
    const mx=Math.max(...ra.by_service.map(s=>s.eur||0),1);
    el.innerHTML='<p class="muted" style="margin:0 0 8px">'+ra.insight+'</p>'+ra.by_service.slice(0,5).map(s=>
      `<div style="display:flex;align-items:center;gap:8px;margin:3px 0"><div style="flex:0 0 160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.service}</div>`+
      `<div style="flex:1;background:#eef2ff;border-radius:4px"><div style="width:${Math.max(4,Math.round((s.eur||0)/mx*100))}%;background:#6366f1;border-radius:4px;color:#fff;font-size:12px;padding:2px 6px;white-space:nowrap">€${s.eur||0} · ${s.n}</div></div></div>`).join('');
  }).catch(()=>{});
  api('risk').then(rk=>{
    const el=$('#ovRisk'); if(!el) return;
    const risky=(rk.high||[]).concat(rk.medium||[]);
    if(!risky.length){ el.innerHTML='<span class="muted">Κανένα αυξημένο ρίσκο ✓ ('+rk.scanned+' ραντεβού ελέγχθηκαν)</span>'; return; }
    el.innerHTML='<table><tbody>'+risky.slice(0,6).map(s=>
      `<tr><td><span style="display:inline-block;padding:2px 9px;border-radius:12px;font-weight:700;background:${s.band==='high'?'#fee2e2':'#fef3c7'};color:${s.band==='high'?'#991b1b':'#92400e'}">${Math.round(s.risk*100)}%</span></td>`+
      `<td><b>${s.name}</b> · ${s.date} ${s.time}</td><td class="muted">${(s.reasons||[]).join(' · ')}</td></tr>`).join('')+
      '</tbody></table><p class="muted" style="font-size:12px;margin-top:6px">Στους high στέλνεται έξτρα υπενθύμιση αυτόματα — αν θες, πάρε και ένα τηλέφωνο.</p>';
  }).catch(()=>{});
  api('channels').then(ch=>{const el=$('#ovChannels'); if(!el) return;
    const chip=(on,l)=>`<span style="display:inline-block;padding:2px 9px;border-radius:12px;margin-right:6px;background:${on?'#d1fae5':'#fee2e2'};color:${on?'#065f46':'#991b1b'};font-weight:600">${on?'✓':'✗'} ${l}</span>`;
    const allOn=ch.email&&ch.sms&&ch.voice;
    el.innerHTML='Ειδοποιήσεις: '+chip(ch.email,'Email')+chip(ch.sms,'SMS')+chip(ch.voice,'Τηλέφωνο')+(allOn?'':'<span class="muted" style="font-size:12px">— τα ✗ ανάβουν μόλις μπουν τα κλειδιά</span>');
  }).catch(()=>{});
  api('forecast').then(fc=>{
    const el=$('#ovForecast'); if(!el) return;
    if(!fc.ready){ el.innerHTML='<span class="muted">'+(fc.reason||'Χρειάζεται περισσότερο ιστορικό.')+'</span>'; return; }
    const mx=Math.max(...fc.days.map(d=>d.expected_bookings),1);
    el.innerHTML=
      '<div style="display:flex;align-items:flex-end;gap:4px;height:74px;margin:8px 0 6px">'+
      fc.days.map(d=>`<div title="${d.weekday} ${d.date}: ~${d.expected_bookings} ραντεβού" style="flex:1;background:${d.date===fc.busiest.date?'#059669':'#a7f3d0'};border-radius:4px 4px 0 0;height:${Math.max(8,Math.round(d.expected_bookings/mx*70))}px"></div>`).join('')+
      '</div><p class="muted" style="font-size:12.5px">'+fc.insight+'</p>'+
      '<p class="muted" style="font-size:11px">μ.ό. ~'+fc.avg_per_day+' ραντεβού/μέρα · βάσει '+fc.history_days+' ημερών ιστορικού · μηχανή: '+(fc.engine==='google-timesfm'?'Google TimesFM':'στατιστική (per-weekday)')+'</p>';
  }).catch(()=>{const el=$('#ovForecast'); if(el) el.innerHTML='<span class="muted">Μη διαθέσιμη.</span>';});
}

async function patients(){
  const d=await api('patients');DATA.pat=d.patients;
  $('#view').innerHTML=`<div class="card"><h3>👥 Φάκελοι Ασθενών (${d.patients.length}) <span style="margin-left:auto;display:flex;gap:8px"><button class="navlink" style="cursor:pointer;background:none" onclick="importPatients()">📥 Εισαγωγή Excel</button><button class="abtn" onclick="newPatient()">+ Νέος ασθενής</button></span></h3>
  <table><thead><tr><th>Όνομα</th><th>Τηλέφωνο</th><th class="right">Επισκέψεις</th><th>Τελευταία</th><th class="right">Υπόλοιπο</th></tr></thead>
  <tbody>${d.patients.map(p=>`<tr><td><a class="link" onclick="openPatient('${p.phone}')">${p.name||'(χωρίς όνομα)'}</a></td><td class="muted">${p.phone}</td><td class="right">${p.visits}</td><td class="muted">${p.last_visit||'—'}</td><td class="right">${p.balance>0.01?'<span class="pill warn">'+eur(p.balance)+'</span>':'<span class="muted">—</span>'}</td></tr>`).join('')}</tbody></table></div>`;
}

async function openPatient(ph){
  const d=await api('patient/'+encodeURIComponent(ph));
  const p=d.profile;
  $('#mbody').innerHTML=`<h2 style="margin-bottom:4px">${p.name||ph}</h2>
  <p class="muted" style="margin-bottom:12px">${ph} ${p.email?'· '+p.email:''} ${d.balance>0.01?'· <span class="pill warn">οφείλει '+eur(d.balance)+'</span>':''}</p>
  <div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">
    <button class="abtn" onclick="addNote('${ph}')">📝 + Σημείωση</button>
    <button class="abtn" onclick="addInvoice('${ph}','${(p.name||'').replace(/'/g,'')}')">🧾 + Τιμολόγιο</button>
  </div>
  <div class="card"><h3>🗂️ Ιστορικό ραντεβού (${d.appointments.length})</h3><table><tbody>${d.appointments.slice(0,8).map(a=>`<tr><td>${a.date} ${a.time}</td><td>${a.service}</td><td>${pill(a.status)}</td></tr>`).join('')||'<tr><td class="muted">—</td></tr>'}</tbody></table></div>
  <div class="card"><h3>📝 Φάκελος (σημειώσεις)</h3>${d.notes.map(n=>`<div class="ev"><div class="h">${n.created} · ${n.author} · <small class="tag">${n.kind}</small></div><b>${n.title||''}</b> ${n.body}</div>`).join('')||'<span class="muted">—</span>'}</div>
  <div class="card"><h3>💶 Τιμολόγια</h3><table><tbody>${d.invoices.map(i=>`<tr><td>${i.number}</td><td>${i.service}</td><td class="right">${eur(i.total)}</td><td>${pill(i.status)}</td></tr>`).join('')||'<tr><td class="muted">—</td></tr>'}</tbody></table></div>
  <div class="card"><h3>💬 Επικοινωνία</h3><div class="tl">${d.communications.map(c=>`<div class="ev"><div class="h">${c.created} · ${c.channel} · ${c.direction}</div>${c.subject?'<b>'+c.subject+'</b> ':''}${c.body}</div>`).join('')||'<span class="muted">—</span>'}</div></div>
  <p class="muted" style="font-size:12px">🔗 ${d.graph.length} διασυνδέσεις στο graph (ραντεβού/τιμολόγια/επικοινωνίες/σημειώσεις)</p>`;
  $('#modal').style.display='flex';
}
function closeM(){$('#modal').style.display='none'}

function guide(){
  $('#mbody').innerHTML=`<h2 style="margin-bottom:8px">👋 Πώς λειτουργεί — σε 1 λεπτό</h2>
  <p class="muted" style="margin-bottom:16px">Το σύστημα δουλεύει <b>μόνο του</b>. Δεν χρειάζεται να μάθεις κάτι. Να τι κάνει:</p>
  <div class="card"><b>1. 📞 Σηκώνει τα τηλέφωνά σου</b><br/>Όσο είσαι με ασθενή, το AI σηκώνει κλήσεις & μηνύματα 24/7, κλείνει ραντεβού, στέλνει SMS επιβεβαίωσης. Εσύ δεν κάνεις τίποτα.</div>
  <div class="card"><b>2. 🔁 Φέρνει πίσω χαμένους</b><br/>Κυνηγάει όσους δεν ήρθαν και όσους ξέχασαν να ξανάρθουν — αυτόματα, με SMS.</div>
  <div class="card"><b>3. 🧠 Κρατάει το αρχείο σου</b><br/>Κάθε ασθενής, ραντεβού & τιμολόγιο μπαίνει μόνο του στον φάκελο. Εδώ απλά τα <b>βλέπεις</b> — κλικ σε ασθενή για όλο το ιστορικό του.</div>
  <div class="card" style="border-color:#bfdbfe;background:#eff6ff"><b>4. 🤖 Ο AI Γραμματέας εκτελεί ό,τι του πεις</b><br/>Στο tab <b>Βοηθός</b> γράφεις σαν να μιλάς σε γραμματέα: «κλείσε ραντεβού στον Γιώργο αύριο 5», «έκδωσε τιμολόγιο €50 στη Μαρία», «θύμισε στους αυριανούς». Σου δείχνει τι θα κάνει → πατάς <b>Έγκριση</b> → γίνεται. Τίποτα δεν εκτελείται χωρίς το ΟΚ σου.</div>
  <div class="card"><b>5. 📋 Σου λέει τι να κάνεις</b><br/>Το tab <b>To-Do</b> σου λέει ποιον να καλέσεις, τι να παραγγείλεις, ποια χρεωστικά να κυνηγήσεις σήμερα.</div>
  <p class="muted">Έχεις παλιούς ασθενείς; Στο tab «Φάκελοι» → <b>📥 Εισαγωγή Excel</b>. Πάνω-δεξιά → το τηλέφωνο (📞 Front-desk) & τα έσοδα (📊).</p>`;
  $('#modal').style.display='flex';
}

async function finance(){
  const d=await api('finance');const f=d.summary;
  $('#view').innerHTML=`<div class="grid">
    <div class="kpi" style="background:linear-gradient(135deg,#ecfdf5,#d1fae5);border-color:#a7f3d0"><div class="n" style="color:#047857">${eur(f.revenue)}</div><div class="l" style="color:#065f46">Εισπράξεις 30ημ</div></div>
    <div class="kpi"><div class="n">${eur(f.billed)}</div><div class="l">Τιμολογημένα 30ημ</div></div>
    <div class="kpi"><div class="n" style="color:var(--warn)">${eur(f.outstanding)}</div><div class="l">Ανοιχτά υπόλοιπα</div></div>
    <div class="kpi"><div class="n">${eur(f.expenses)}</div><div class="l">Έξοδα 30ημ</div></div>
    <div class="kpi"><div class="n">${f.mydata_pending}</div><div class="l">τιμολόγια για λογιστή</div></div></div>
  <div class="card"><h3>🔴 Ποιος μου χρωστάει</h3><table><tbody>${d.outstanding.map(o=>`<tr><td>${o.patient_name||o.patient_phone}</td><td class="muted">${o.patient_phone}</td><td class="right"><span class="pill warn">${eur(o.owed)}</span></td></tr>`).join('')||'<tr><td class="muted">Κανένα ανοιχτό υπόλοιπο ✓</td></tr>'}</tbody></table></div>
  <div class="card"><h3>🧾 Τιμολόγια</h3><table><thead><tr><th>Αρ.</th><th>Ημ/νία</th><th>Ασθενής</th><th>Υπηρεσία</th><th class="right">Σύνολο</th><th>Status</th><th></th></tr></thead><tbody>${d.invoices.map(i=>`<tr><td>${i.number}</td><td class="muted">${i.issue_date}</td><td>${i.patient_name||'—'}</td><td>${i.service}</td><td class="right">${eur(i.total)}</td><td>${pill(i.status)}</td><td class="right">${i.status!=='paid'?`<button class="miniact" onclick="payInv(${i.id})">✓ Πληρώθηκε</button>`:'<span class="muted">—</span>'}</td></tr>`).join('')}</tbody></table></div>`;
}

async function inventory(){
  const d=await api('inventory');
  $('#view').innerHTML=`<div class="card"><h3>⚠️ Χρειάζονται παραγγελία (${d.low_stock.length})</h3><table><tbody>${d.low_stock.map(i=>`<tr><td>${i.name}</td><td><span class="pill bad">${i.qty} ${i.unit}</span> / όριο ${i.reorder_level}</td><td class="muted">${i.supplier||'—'}</td></tr>`).join('')||'<tr><td class="muted">Όλα επαρκή ✓</td></tr>'}</tbody></table></div>
  <div class="card"><h3>📦 Αποθήκη (${d.items.length} είδη)</h3><table><thead><tr><th>Είδος</th><th>Κατηγορία</th><th class="right">Απόθεμα</th><th class="right">Όριο</th><th>Προμηθευτής</th></tr></thead><tbody>${d.items.map(i=>`<tr><td>${i.name}</td><td class="muted">${i.category}</td><td class="right">${i.qty<=i.reorder_level?'<span class="pill bad">'+i.qty+' '+i.unit+'</span>':i.qty+' '+i.unit}</td><td class="right muted">${i.reorder_level}</td><td class="muted">${i.supplier||'—'}</td></tr>`).join('')}</tbody></table></div>
  <div class="card"><h3>🚚 Προμηθευτές</h3><table><tbody>${d.suppliers.map(s=>`<tr><td><b>${s.name}</b></td><td class="muted">${s.category}</td><td class="muted">${s.phone} ${s.email}</td></tr>`).join('')||'<tr><td class="muted">—</td></tr>'}</tbody></table></div>`;
}

async function tasks(){
  const d=await api('tasks');
  $('#view').innerHTML=`<div class="card"><h3>✅ To-Do Γραμματείας — αυτόματα από τους agents (${d.tasks.length})</h3>
  <table><thead><tr><th>Προτ.</th><th>Εργασία</th><th>Τύπος</th><th></th></tr></thead><tbody>${d.tasks.map(t=>`<tr><td>${pill(t.priority)}</td><td>${t.title}</td><td class="muted">${t.kind}</td><td class="right"><button class="miniact" onclick="taskDone(${t.id})">✓ Έγινε</button></td></tr>`).join('')||'<tr><td colspan=4 class="muted">Όλα τακτοποιημένα ✓</td></tr>'}</tbody></table></div>`;
}

async function agents(){
  const d=await api('agents');
  $('#view').innerHTML=`<div class="card"><h3>🤖 Back-Office Agents (${d.active}/${d.total_agents} ενεργοί)</h3>
  <p class="muted" style="margin-bottom:14px">Κάθε agent αντικαθιστά μια χειροκίνητη λειτουργία του laptop. Συνδέονται μεταξύ τους μέσω κοινού data layer — σαν Obsidian.</p>
  ${d.agents.map(a=>`<div class="agent"><div class="dot"></div><div><div class="nm">${a.name}</div><div class="ro">${a.role}</div><div class="ro" style="margin-top:6px">↪ <b>Αντικαθιστά:</b> ${a.replaces}</div><div class="meta">⚡ ${a.trigger} · 📊 ${a.metric} · 🔌 ${a.connectors.join(', ')}</div></div></div>`).join('')}</div>`;
}

async function manageSub(){
  try{
    const r=await fetch(`/api/${SLUG}/billing/portal?t=${T}`,{method:'POST'}).then(x=>x.json());
    if(r.url){ window.location.href=r.url; return; }
    alert(r.msg||r.error||'Δεν βρέθηκε συνδρομή για διαχείριση.');
  }catch(e){ alert('Σφάλμα δικτύου.'); }
}
let CHAT=[];  // {role, text}
function assistant(){
  $('#view').innerHTML=`
  <div class="card" style="padding:0;overflow:hidden">
    <div style="padding:14px 18px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px">
      <b>🤖 AI Γραμματέας</b>
      <span class="muted" style="font-size:12px">— γράψε ό,τι θα έλεγες στη γραμματέα σου</span>
    </div>
    <div id="chatlog" style="padding:16px 18px;min-height:240px;max-height:50vh;overflow:auto"></div>
    <div style="padding:12px 18px;border-top:1px solid var(--line);display:flex;gap:8px">
      <input id="chatin" placeholder="π.χ. κλείσε ραντεβού στον Γιώργο 6985... αύριο στις 5"
             style="flex:1;background:#fff;border:1px solid var(--line);padding:11px 13px;border-radius:10px;font-size:14px;font-family:inherit"/>
      <button class="abtn" id="chatsend" onclick="chatSend()">Στείλε</button>
    </div>
  </div>
  <p class="muted" style="font-size:12px">💡 <b>Ρώτα:</b> «τι έχω σήμερα;» · «ποιος μου χρωστάει;» · «πώς πάμε;» · «δείξε μου τον Γιώργο» · «τι να παραγγείλω;»<br/>💡 <b>Ή πες να κάνει:</b> «κλείσε ραντεβού στον Πέτρο 6985... αύριο 5» · «έκδωσε τιμολόγιο €50 στη Μαρία» · «θύμισε σε όσους δεν ήρθαν 3 μήνες»</p>
  <div style="margin-top:6px"><button class="navlink" onclick="sendBriefingNow()" style="cursor:pointer;background:#f1f5f9;border:1px solid var(--line);border-radius:8px;padding:7px 12px;font-size:13px">📋 Στείλε μου τη σύνοψη τώρα</button> <span id="briefmsg" class="muted" style="font-size:12px"></span></div>`;
  renderChat();
  $('#chatin').addEventListener('keydown',e=>{ if(e.key==='Enter') chatSend(); });
  $('#chatin').focus();
  if(!CHAT.length) proactiveSnapshot();
}
async function proactiveSnapshot(){
  // HERO = ΛΕΦΤΑ, όχι vanity: πόσα ανέκτησε ο AI (με ΟΝΟΜΑΤΑ = αδιαμφισβήτητο) + πόσα € «στο τραπέζι τώρα».
  try{
    const [s,t]=await Promise.all([api('summary'),api('tasks')]);
    const rec=s.recovered||{}, mot=s.money_on_table||{}, evs=s.recovered_events||[];
    const today=(s.calendar_today||{}).appointments||0;
    const recEur=Math.round(rec.recovered_eur||0), atRisk=Math.round(mot.at_risk_eur||0);
    const lines=[];
    if(recEur>0){
      const names=evs.slice(0,3).map(e=>`${((e.name||'').split(' ')[0])||'ασθενής'} (€${Math.round(e.value||0)})`).join(', ');
      lines.push(`💰 Σου γύρισα πίσω <b>€${recEur}</b> από <b>${rec.recovered_count||0}</b> ασθενείς${names?' — '+names:''}`);
    }
    if(atRisk>0){
      lines.push(`⚠️ <b>€${atRisk}</b> στο τραπέζι ΤΩΡΑ — ${mot.debtor_count||0} χρωστάνε, ${mot.dormant_count||0} αδρανείς ασθενείς. Θες να τους φέρω πίσω;`);
    }
    lines.push(today>0?`📅 <b>${today}</b> ραντεβού σήμερα`:'📅 ήσυχη μέρα σήμερα');
    const hi = (recEur>0||atRisk>0)
      ? `Καλημέρα! ${lines.join('<br/>')}<br/>Τι θες να κάνουμε;`
      : `Καλημέρα! 📅 ${today} ραντεβού σήμερα. Μόλις δουλέψει ο βοηθός, εδώ θα βλέπεις <b>πόσα € σου γυρνάω πίσω</b> (με ονόματα). Τι θες να κάνουμε;`;
    if(!CHAT.length){ CHAT.push({role:'assistant',text:''}); CHAT[0]._html=hi; renderChat(); }
  }catch(e){}
}
async function sendBriefingNow(){
  const m=$('#briefmsg'); if(m) m.textContent='Στέλνω…';
  try{ const r=await post('briefing/send',{});
    if(m) m.textContent = r.ok? ('✓ '+r.msg) : ('⚠️ '+(r.msg||r.detail||'Σφάλμα'));
  }catch(e){ if(m) m.textContent='⚠️ Σφάλμα δικτύου'; }
}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function renderChat(){
  const log=$('#chatlog'); if(!log) return;
  log.innerHTML = CHAT.length? CHAT.map((m,i)=>{
    if(m.role==='user') return `<div style="text-align:right;margin:8px 0"><span style="display:inline-block;background:#eff6ff;color:#1e40af;padding:8px 12px;border-radius:12px;max-width:80%">${esc(m.text)}</span></div>`;
    let acts='';
    if(m.actions&&m.actions.length){
      acts=`<div style="margin-top:10px;background:#f8fafc;border:1px solid var(--line);border-radius:10px;padding:10px">
        <div class="muted" style="font-size:12px;margin-bottom:6px">Θα γίνουν μετά την έγκρισή σου:</div>
        ${m.actions.map(a=>`<div style="padding:4px 0">• ${esc(a.label)}</div>`).join('')}
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="abtn" onclick="approve(${i})" ${m.done?'disabled':''}>${m.done?'✓ Εγκρίθηκε':'✅ Έγκριση'}</button>
          <button class="navlink" onclick="reject(${i})" ${m.done?'disabled':''} style="cursor:pointer;background:none">Άκυρο</button>
        </div>
        ${m.result?`<div style="margin-top:8px;color:var(--ok);font-size:13px">${esc(m.result)}</div>`:''}
      </div>`;
    }
    return `<div style="margin:8px 0"><span style="display:inline-block;background:#f1f5f9;padding:8px 12px;border-radius:12px;max-width:88%">${m._html||esc(m.text)}</span>${acts}</div>`;
  }).join('') : '<p class="muted">Ξεκίνα γράφοντας ένα αίτημα…</p>';
  log.scrollTop=log.scrollHeight;
}
async function chatSend(){
  const inp=$('#chatin'); const q=(inp.value||'').trim(); if(!q) return;
  inp.value=''; CHAT.push({role:'user',text:q});
  CHAT.push({role:'assistant',text:'…σκέφτομαι'}); renderChat();
  const hist=CHAT.filter(m=>m.text&&m.text!=='…σκέφτομαι').slice(0,-1).map(m=>({role:m.role,text:m.text}));
  let r; try{ r=await post('ask',{question:q,history:hist}); }catch(e){ r={error:'σφάλμα δικτύου'}; }
  CHAT.pop(); // βγάλε το "σκέφτομαι"
  const _aid=()=>(window.crypto&&crypto.randomUUID?crypto.randomUUID():String(Date.now())+'-'+Math.random());
  if(r.error){ CHAT.push({role:'assistant',text:'⚠️ '+(r.detail||r.error)}); }
  else { CHAT.push({role:'assistant',text:r.answer,actions:(r.proposed_actions||[]).map(a=>({...a,aid:_aid()}))}); }
  renderChat();
}
async function approve(i){
  const m=CHAT[i]; if(!m||!m.actions) return;
  m.done=true; m.result='Εκτελείται…'; renderChat();
  let r; try{ r=await post('execute',{actions:m.actions.map(a=>({tool:a.tool,input:a.input,aid:a.aid}))}); }catch(e){ r={error:'σφάλμα δικτύου'}; }
  m.result = r.ok? ('✓ '+(r.msg||'Έγινε.')) : ('⚠️ '+((r.results&&r.results.map(x=>x.error||x.msg).join('; '))||r.error||r.msg||'Σφάλμα'));
  renderChat();
}
function reject(i){ const m=CHAT[i]; if(m){ m.done=true; m.result='Ακυρώθηκε.'; renderChat(); } }

function importPatients(){
  $('#mbody').innerHTML=`<h2 style="margin-bottom:8px">📥 Εισαγωγή παλιών ασθενών</h2>
  <p class="muted" style="margin-bottom:14px">Ανέβασε αρχείο <b>.xlsx</b> ή <b>.csv</b> με στήλες <b>Όνομα</b>, <b>Τηλέφωνο</b>, Email (προαιρετικό) και <b>Τελευταία επίσκεψη</b> (προαιρετικό — αλλά ενεργοποιεί το win-back: βρίσκει ποιοι δεν ήρθαν εδώ και καιρό). Αναγνωρίζει ελληνικές & αγγλικές επικεφαλίδες.</p>
  <input id="impfile" type="file" accept=".xlsx,.csv" style="width:100%;margin-bottom:12px"/>
  <button class="abtn" id="impgo" style="width:100%">Εισαγωγή</button>
  <div class="muted" id="impmsg" style="margin-top:12px;text-align:center"></div>`;
  $('#modal').style.display='flex';
  $('#impgo').onclick=async()=>{
    const f=$('#impfile').files[0];
    if(!f){ $('#impmsg').textContent='Διάλεξε αρχείο πρώτα.'; return; }
    $('#impgo').disabled=true; $('#impmsg').textContent='Εισαγωγή…';
    const fd=new FormData(); fd.append('file',f);
    let r; try{ r=await fetch(`/api/${SLUG}/back-office/import-patients?t=${T}`,{method:'POST',body:fd}).then(x=>x.json()); }
    catch(e){ r={error:'σφάλμα δικτύου'}; }
    if(r.ok){ $('#impmsg').innerHTML='<span style="color:var(--ok)">✓ '+r.msg+'</span>'; setTimeout(()=>{closeM();patients();},1200); }
    else { $('#impmsg').innerHTML='<span style="color:var(--bad)">⚠️ '+(r.error||'Σφάλμα')+'</span>'; $('#impgo').disabled=false; }
  };
}

// Έσοδα & Ρυθμίσεις: ενσωματωμένες μέσα στη ΜΙΑ σελίδα (ίδιο ?t= token) — ο γιατρός δεν φεύγει.
function esodaView(){ $('#view').innerHTML=`<iframe title="Έσοδα" src="/${SLUG}/dashboard?t=${T}" style="width:100%;height:80vh;border:1px solid var(--line);border-radius:12px;background:#fff"></iframe>`; }
function setupView(){ $('#view').innerHTML=`<iframe title="Ρυθμίσεις" src="/${SLUG}/setup?t=${T}" style="width:100%;height:80vh;border:1px solid var(--line);border-radius:12px;background:#fff"></iframe>`; }

const TABS={day,overview,autos,patients,finance,esodaView,inventory,setupView,assistant};
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{
  stopDayPoll();   // βγαίνοντας από την «Ημέρα» σταμάτα το live refresh
  document.querySelectorAll('#tabs button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');TABS[b.dataset.t]();
});
$('#agentsBadge').textContent='🤖 AI ενεργός';
day();   // η καθημερινή οθόνη της γραμματείας (ραντεβού/check-in/ταμείο) είναι το default
try{ if(!localStorage.getItem('rt_guide_seen')){ guide(); localStorage.setItem('rt_guide_seen','1'); } }catch(e){}
</script></body></html>"""
