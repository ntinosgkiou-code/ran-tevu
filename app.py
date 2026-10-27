import os, json, secrets, functools, threading, time as _time, hmac, hashlib
import sys as _sys
from datetime import datetime, date, timedelta

# UTF-8 stdout: import-time prints contain Greek/emoji; on a non-UTF-8 console (Windows cp1253)
# an emoji print raised UnicodeEncodeError and silently aborted blueprint imports. Harmless on
# Linux/Railway (already UTF-8) but removes a real local-dev footgun.
try:
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── Timezone: Greece (UTC+2/+3 DST) ──────────────────────────────────────────
os.environ.setdefault("TZ", "Europe/Athens")
try:
    _time.tzset()   # Unix/Linux only — no-op on Windows
except AttributeError:
    pass

from flask import Flask, request, jsonify, send_from_directory, abort, redirect
import anthropic
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import pathlib

# ── Load .env ─────────────────────────────────────────────────────────────────
_env_path = pathlib.Path(__file__).parent / ".env"
if _env_path.exists():
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if "=" in _line and not _line.startswith("#"):
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from database import (
    init_db, get_business, list_businesses, create_business, update_business, rename_slug,
    get_available_slots, create_booking, get_all_bookings, cancel_booking,
    get_booking, get_client_profile, upsert_client_profile, update_client_after_booking,
    get_pending_reminders, mark_reminder_sent, mark_risk_reminder_sent, find_bookings_by_phone,
    cancel_booking_by_phone, reschedule_booking,
    get_bookings_awaiting_outcome, set_booking_pending_outcome, mark_booking_outcome,
    enqueue_outbound_job,
    add_to_waitlist as db_add_to_waitlist, pop_first_waiter,
    get_clients_for_reengagement, get_days_with_few_bookings,
    mark_outreach_sent, get_clients_with_email,
    get_bookings_for_questionnaire, get_bookings_for_review,
    mark_questionnaire_sent, mark_review_sent,
    get_weekly_stats,
    is_unsubscribed, add_unsubscribe, list_unsubscribes,
    was_sent, mark_sent, count_sent,
    add_pending_claim, get_pending_claim, mark_claim_used,
    # Memory + compliance (future-proof additions)
    get_customer_memory, upsert_customer_memory,
    export_customer_data, delete_all_customer_data,
    log_audit, log_consent, list_audit_logs,
)
from memory_service import (
    fetch_memory_for_prompt, store_conversation_memory,
    render_memory_section, extract_identifier,
)
from email_service import (
    send_confirmation, send_reminder,
    send_fill_slots_email, send_reengagement_email,
    send_questionnaire, send_review_request,
    send_cancellation_to_client, send_setup_confirmation,
    send_weekly_report, send_waitlist_offer,
)

try:
    from calendar_service import add_to_calendar
except Exception:
    def add_to_calendar(booking): return None

try:
    from sms_service import send_sms, sms_reminder
except Exception:
    def send_sms(phone, msg): return False
    def sms_reminder(booking, hours): return False

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024

BASE_PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "autonomous": autonomous_status(),
        "scheduler": {
            "running": _scheduler.running,
            "jobs": _scheduler_status(),
        },
    })


@app.route("/api/clinic/autonomous/status")
def autonomous_status_route():
    return jsonify(autonomous_status())


@app.route("/api/clinic/autonomous/trigger", methods=["POST"])
def autonomous_trigger_route():
    try:
        _autonomous_tick()
        return jsonify({"ok": True, "status": autonomous_status()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/hub")
def hub_page():
    return send_from_directory(BASE_PUBLIC_DIR, "hub.html")


# /demo, /demo/back-office, /demo/dashboard ΑΦΑΙΡΕΘΗΚΑΝ — σέρβιραν παλιές ΣΤΑΤΙΚΕΣ σελίδες
# (index.html/admin.html/admin_dashboard_v2.html) που ΣΚΙΑΖΑΝ το πραγματικό προϊόν.
# Τώρα πέφτουν στα δυναμικά /<slug> routes για το slug 'demo' (DEMO_SLUG), token-free:
#   /demo               → chat_page('demo')        = σελίδα κράτησης ασθενή
#   /demo/back-office    → back_office_page('demo') = το ΑΛΗΘΙΝΟ back-office με ΟΛΑ τα tabs
#   /demo/dashboard      → customer_dashboard('demo') = dashboard εσόδων (demo-skip token)


@app.route("/admin/integrations")
def admin_integrations_page():
    return send_from_directory(BASE_PUBLIC_DIR, "admin_integrations.html")


@app.route("/admin/testing")
def admin_testing_page():
    return send_from_directory(BASE_PUBLIC_DIR, "admin.html")


@app.route("/admin/architecture")
def admin_architecture_page():
    return send_from_directory(BASE_PUBLIC_DIR, "about.html")

# ── Autonomous clinic runtime ───────────────────────────────────────────────
_AUTONOMOUS_STATE = {
    "running": False,
    "started_at": None,
    "last_run": None,
    "runs": 0,
    "errors": [],
}
_AUTONOMOUS_LOCK = threading.Lock()
_AU_THREAD = None


def _autonomous_tick():
    """Μία μικρή autonomous ροή για το ιατρείο: reminders, outcomes, waitlist/recovery hooks."""
    try:
        check_reminders()
        check_booking_outcomes()
        auto_infer_no_shows()
        fill_empty_slots()
        reengage_inactive_clients()
    except Exception as exc:
        _AUTONOMOUS_STATE["errors"].append(str(exc)[:400])
        if len(_AUTONOMOUS_STATE["errors"]) > 10:
            _AUTONOMOUS_STATE["errors"] = _AUTONOMOUS_STATE["errors"][-10:]


def _autonomous_loop():
    while True:
        try:
            with _AUTONOMOUS_LOCK:
                _AUTONOMOUS_STATE["running"] = True
                _AUTONOMOUS_STATE["last_run"] = datetime.now().isoformat(timespec="seconds")
                _AUTONOMOUS_STATE["runs"] += 1
            _autonomous_tick()
        except Exception as exc:
            _AUTONOMOUS_STATE["errors"].append(str(exc)[:400])
        _time.sleep(60)


def start_autonomous_runtime():
    global _AU_THREAD
    with _AUTONOMOUS_LOCK:
        if _AU_THREAD and _AU_THREAD.is_alive():
            return False
        _AUTONOMOUS_STATE["started_at"] = datetime.now().isoformat(timespec="seconds")
        _AU_THREAD = threading.Thread(target=_autonomous_loop, daemon=True, name="clinic-autonomous")
        _AU_THREAD.start()
        return True


def autonomous_status():
    return {
        "running": _AUTONOMOUS_STATE["running"],
        "started_at": _AUTONOMOUS_STATE["started_at"],
        "last_run": _AUTONOMOUS_STATE["last_run"],
        "runs": _AUTONOMOUS_STATE["runs"],
        "errors": _AUTONOMOUS_STATE["errors"][-5:],
    }

claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"),
                             timeout=30.0, max_retries=2)   # μη κρεμάς worker thread ώς 10′ αν κολλήσει το API

limiter = Limiter(app=app, key_func=get_remote_address, default_limits=[], storage_uri="memory://")

init_db()
start_autonomous_runtime()

try:
    from database import quarantine_generic_emails
    _qstats = quarantine_generic_emails()
    if _qstats.get("quarantined", 0) > 0:
        print(f"[EMAIL_HYGIENE] quarantined {_qstats['quarantined']} generic emails (info@/contact@/...)")
except Exception as _qerr:
    print(f"[EMAIL_HYGIENE] quarantine skipped: {_qerr}")

# ── Growth blueprint (content marketing → social → Ran-Tevu signups) ─────────
try:
    from routes.growth_routes import register as _register_growth
    _register_growth(app)
    print("[GROWTH] blueprint loaded")
except Exception as _growth_err:
    print(f"[GROWTH] blueprint not loaded: {_growth_err}")

# ── Voice blueprint (Vapi + Twilio + ElevenLabs voice receptionist) ──────────
try:
    from routes.voice_routes import register as _register_voice
    _register_voice(app)
    print("[VOICE] blueprint loaded")
except Exception as _voice_err:
    print(f"[VOICE] blueprint not loaded: {_voice_err}")

# ── Agentic Commerce blueprint (Feature #4: AI-to-AI bookings via MCP/OpenAPI) ──
try:
    from routes.agentic_commerce import bp as _agentic_bp
    app.register_blueprint(_agentic_bp)
    print("[AGENTIC_COMMERCE] blueprint loaded — /openapi.json /.well-known/ai-plugin.json")
except Exception as _ac_err:
    print(f"[AGENTIC_COMMERCE] blueprint not loaded: {_ac_err}")

# ── Unified Inbox blueprint (Feature #2: WhatsApp + Instagram + Booking) ──────
try:
    from routes.inbox import bp as _inbox_bp
    app.register_blueprint(_inbox_bp)
    print("[INBOX] blueprint loaded — /webhooks/whatsapp /webhooks/instagram")
except Exception as _inbox_err:
    print(f"[INBOX] blueprint not loaded: {_inbox_err}")

# ── Back-Office blueprint (φάκελοι ασθενών, myDATA, αποθήκη, agents — Obsidian) ─
try:
    from back_office import init_back_office as _init_bo
    _init_bo()
    from routes.back_office_routes import register as _register_bo
    _register_bo(app)
except Exception as _bo_err:
    print(f"[BACK_OFFICE] blueprint not loaded: {_bo_err}")

# ── Self-serve instant demo (/start) — στήνεται μόνο του ──────────────────────
try:
    from routes.start_routes import register as _register_start
    _register_start(app)
except Exception as _start_err:
    print(f"[START] blueprint not loaded: {_start_err}")

# ── Newsletter «Ιατρική Νοημοσύνη» (opt-in εγγραφές + admin αποστολή τεύχους) ──
try:
    from routes.newsletter_routes import register as _register_newsletter
    _register_newsletter(app)
except Exception as _nl_err:
    print(f"[NEWSLETTER] blueprint not loaded: {_nl_err}")

# ── Cold outreach queue (personalized Rigoudis emails, server cron 25/day) ────
try:
    import cold_outreach as _cold
    _cold.init_cold()
    print("[COLD] queue ready — daily send 09:00")
except Exception as _cold_err:
    print(f"[COLD] init failed: {_cold_err}")

# ── Blog blueprint (SEO + content marketing) ─────────────────────────────────
try:
    from routes.blog_routes import register as _register_blog
    _register_blog(app)
    print("[BLOG] blueprint loaded")
except Exception as _blog_err:
    print(f"[BLOG] blueprint not loaded: {_blog_err}")

# ── Physio Email Cron (Railway server → cron-job.org daily trigger) ──────────
try:
    from routes.physio_cron import register as _register_physio_cron
    _register_physio_cron(app)
except Exception as _physio_cron_err:
    print(f"[PHYSIO_CRON] blueprint not loaded: {_physio_cron_err}")

# ── Billing blueprint (recurring Stripe subscription €179/μήνα + 14d trial) ──
try:
    from routes.billing_routes import register as _register_billing
    _register_billing(app)
    print("[BILLING] blueprint loaded — /api/billing/checkout /webhooks/stripe")
except Exception as _billing_err:
    print(f"[BILLING] blueprint not loaded: {_billing_err}")

# ── Unified Dashboard blueprint (admin hub) ──────────────────────────────────
try:
    from routes.dashboard_routes import register as _register_dashboard
    _register_dashboard(app)
    print("[DASHBOARD] blueprint loaded")
except Exception as _dash_err:
    print(f"[DASHBOARD] blueprint not loaded: {_dash_err}")

# ── Agentic Employee panel (propose→approve action queue) ────────────────────
try:
    from routes.agent_routes import register as _register_agent
    _register_agent(app)
    print("[AGENT] agentic panel loaded — /<slug>/agent")
except Exception as _agent_err:
    print(f"[AGENT] blueprint not loaded: {_agent_err}")

# ── Toolkit blueprint (Leads + Pipeline + Campaigns + Integrations) ──────────
try:
    from routes.toolkit_routes import register as _register_toolkit
    _register_toolkit(app)
    print("[TOOLKIT] blueprint loaded")
except Exception as _tk_err:
    print(f"[TOOLKIT] blueprint not loaded: {_tk_err}")

# ── Analytics blueprint (self-hosted, privacy-first tracking) ────────────────
try:
    from routes.analytics_routes import register as _register_analytics
    _register_analytics(app)
    print("[ANALYTICS] blueprint loaded")
except Exception as _an_err:
    print(f"[ANALYTICS] blueprint not loaded: {_an_err}")

# ── FHIR blueprint (διαλειτουργικότητα — το ιατρείο ως FHIR R4 endpoint) ─────
try:
    from routes.fhir_routes import register as _register_fhir
    _register_fhir(app)
except Exception as _fhir_err:
    print(f"[FHIR] blueprint not loaded: {_fhir_err}")

# ── Testing dashboard (οπτική σελίδα ελέγχου για τον ιδιοκτήτη) ───────────────
try:
    from routes.testing_routes import register as _register_testing
    _register_testing(app)
except Exception as _test_err:
    print(f"[TESTING] blueprint not loaded: {_test_err}")

# ── Architecture map (ζωντανός τεχνικός χάρτης + dependency graph) ────────────
try:
    from routes.architecture_routes import register as _register_arch
    _register_arch(app)
except Exception as _arch_err:
    print(f"[ARCHITECTURE] blueprint not loaded: {_arch_err}")

# ── Master admin (για προσθήκη/διαχείριση επιχειρήσεων) ──────────────────────
MASTER_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def _admin_ok(token: str) -> bool:
    """Constant-time master-admin check. Empty ADMIN_TOKEN → ALWAYS reject (never allow blank auth).
    Replaces scattered `token != MASTER_TOKEN` (timing-attackable + would pass when both empty)."""
    return bool(MASTER_TOKEN) and hmac.compare_digest(token or "", MASTER_TOKEN)


_SIGNING_WARNED = False


def _signing_secret() -> bytes:
    """Σταθερό secret για όλα τα HMAC links. ADMIN_TOKEN → SECRET_KEY → (prod: ουρλιάζει).

    ΟΧΙ διαφορετικά hardcoded fallbacks ανά λειτουργία (forgeable): το namespacing
    γίνεται μέσα στο message (booking:/claim:/unsub:), όχι στο key."""
    global _SIGNING_WARNED
    secret = MASTER_TOKEN or os.environ.get("SECRET_KEY", "")
    if not secret:
        if (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_ENVIRONMENT")) and not _SIGNING_WARNED:
            print("[SECURITY ⚠️] ADMIN_TOKEN & SECRET_KEY λείπουν σε production — "
                  "τα HMAC tokens (booking/claim/unsub links) είναι forgeable. Όρισέ τα στο Railway.")
            _SIGNING_WARNED = True
        secret = "ran-tevu-dev-only-insecure"  # local dev μόνο
    return secret.encode()


def _booking_token(booking_id: int, action: str) -> str:
    """HMAC token για confirm/cancel/questionnaire links σε emails."""
    return hmac.new(_signing_secret(), f"{booking_id}:{action}".encode(), hashlib.sha256).hexdigest()[:20]


def _base_url() -> str:
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("BASE_URL", "")
    if domain:
        return f"https://{domain}" if not domain.startswith("http") else domain
    return "http://localhost:5001"


def require_master(f):
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        # Fail-CLOSED: αν λείπει το ADMIN_TOKEN στον server → άρνηση, ΟΧΙ σιωπηλό bypass.
        if not MASTER_TOKEN:
            return jsonify({"error": "Server auth not configured"}), 503
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {MASTER_TOKEN}"):  # constant-time
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapped


def require_business_admin(f):
    """Ελέγχει το admin_token ανά επιχείρηση."""
    @functools.wraps(f)
    def wrapped(slug, *args, **kwargs):
        business = get_business(slug)
        if not business:
            return jsonify({"error": "Business not found"}), 404
        token = business.get("admin_token", "")
        auth = request.headers.get("Authorization", "")
        # Fail-closed + constant-time: business χωρίς admin_token Ή λάθος token → 401
        if not token or not hmac.compare_digest(auth, f"Bearer {token}"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(slug, *args, **kwargs)
    return wrapped


# ════════════════════════════════════════════════════════════════════
#  SCHEDULERS
# ════════════════════════════════════════════════════════════════════

def check_reminders():
    for hours in [24, 4]:
        try:
            bookings = get_pending_reminders(hours)
            for booking in bookings:
                # Mark FIRST atomically — if another process already marked it, skip
                if not mark_reminder_sent(booking["id"], hours):
                    continue
                business = get_business(booking["slug"])
                if not business:
                    continue
                # Generate confirm/cancel URLs for 24h reminder only
                confirm_url = cancel_url = ""
                if hours == 24:
                    base = _base_url()
                    bid  = booking["id"]
                    slug = booking["slug"]
                    confirm_url = f"{base}/api/{slug}/confirm/{bid}?t={_booking_token(bid, 'confirm')}"
                    cancel_url  = f"{base}/api/{slug}/cancel-booking/{bid}?t={_booking_token(bid, 'cancel')}"
                # Στείλε σε ΟΣΑ κανάλια υπάρχουν + κράτα αν έφυγε ΕΣΤΩ ΕΝΑ.
                sent = False
                if (booking.get("email") or "").strip():
                    try:
                        # sent=True ΜΟΝΟ αν έφυγε όντως email (αλλιώς πέφτει στο owner-fallback)
                        if send_reminder(booking, hours, business, confirm_url, cancel_url):
                            sent = True
                    except Exception as e:
                        print(f"[REMINDER] email failed booking {booking['id']}: {e}")
                try:
                    if sms_reminder(booking, hours_before=hours):
                        sent = True
                except Exception as e:
                    print(f"[REMINDER] sms failed booking {booking['id']}: {e}")
                # Κανένα ψηφιακό κανάλι (π.χ. voice booking χωρίς email + SMS μη ρυθμισμένο)
                # → ΜΗΝ χαθεί σιωπηλά (το booking είναι ήδη marked). Ειδοποίησε τον ιδιοκτήτη.
                if not sent:
                    try:
                        from email_service import send_owner_notification
                        send_owner_notification(booking["slug"], {
                            "type": "reminder_unreachable",
                            "customer": booking.get("name", ""), "date": booking.get("date", ""),
                            "time": booking.get("time", ""), "service": booking.get("service", ""),
                            "detail": f"Δεν υπάρχει email/SMS για υπενθύμιση {hours}h — κάλεσέ τον/την τηλεφωνικά.",
                        })
                    except Exception as e:
                        print(f"[REMINDER] owner alert failed booking {booking['id']}: {e}")
        except Exception as e:
            print(f"[SCHEDULER ERROR] {hours}h reminders: {e}")


def check_risk_reminders():
    """Risk-based escalated reminder (risk_brain): high-risk ραντεβού ~2 μέρες πριν παίρνουν ΜΙΑ
    έξτρα υπενθύμιση (πάνω από τα 24h/4h). Idempotent μέσω bookings.reminder_risk. Χωρίς SMS keys
    πέφτει στο email — τίποτα δεν χάνεται σιωπηλά (ίδιο pattern με check_reminders)."""
    try:
        from risk_brain import scan_upcoming
        for biz in list_businesses():
            slug = biz["slug"] if isinstance(biz, dict) else biz
            try:
                scan = scan_upcoming(slug, horizon_days=3)
                for s in scan.get("high", []):
                    bid = s.get("booking_id")
                    if not bid or not mark_risk_reminder_sent(bid):
                        continue            # ήδη στάλθηκε / race → ακριβώς μία φορά
                    b = get_booking(bid)
                    business = get_business(slug)
                    if not (b and business):
                        continue
                    msg = (f"Υπενθύμιση: {b['date']} στις {b['time']} έχετε ραντεβού "
                           f"({b.get('service', '')}). Αν δεν σας βολεύει, πείτε μας να δώσουμε "
                           f"τη θέση — θα βρούμε νέα ώρα εύκολα. {business.get('name', '')}")
                    sent = False
                    try:
                        if send_sms(b.get("phone", ""), msg):
                            sent = True
                    except Exception as e:
                        print(f"[RISK-REMINDER] sms failed booking {bid}: {e}")
                    if not sent and (b.get("email") or "").strip():
                        try:
                            from email_service import send_reminder
                            if send_reminder(b, 48, business, "", ""):
                                sent = True
                        except Exception as e:
                            print(f"[RISK-REMINDER] email failed booking {bid}: {e}")
                    print(f"[RISK-REMINDER] booking {bid} ({s['name']}, risk {s['risk']}) → "
                          f"{'sent' if sent else 'no channel'} · λόγοι: {'; '.join(s['reasons'])}")
            except Exception as e:
                print(f"[RISK-REMINDER] scan failed for {slug}: {e}")
    except Exception as e:
        print(f"[SCHEDULER ERROR] risk reminders: {e}")


def send_questionnaires():
    """Στέλνει pre-visit questionnaire σε bookings που έγιναν 1-3h πριν."""
    try:
        bookings = get_bookings_for_questionnaire()
        base = _base_url()
        for b in bookings:
            # Mark FIRST atomically
            if not mark_questionnaire_sent(b["id"]):
                continue
            business = get_business(b["slug"])
            if not business:
                continue
            token = _booking_token(b["id"], "questionnaire")
            q_url = f"{base}/q/{b['slug']}/{b['id']}?t={token}"
            send_questionnaire(b, business, q_url)
    except Exception as e:
        print(f"[QUESTIONNAIRE SCHEDULER ERROR] {e}")


def check_booking_outcomes():
    """Outbound P2: ραντεβού που πέρασαν χωρίς καταγεγραμμένο αποτέλεσμα →
    pending_outcome + one-tap SMS στον ιδιοκτήτη («ήρθε / δεν ήρθε»)."""
    try:
        base = _base_url()
        for b in get_bookings_awaiting_outcome():
            if not set_booking_pending_outcome(b["id"]):
                continue  # το πήρε άλλο process — race-safe
            business = get_business(b["slug"])
            if not business:
                continue
            yes_url = f"{base}/o/{b['id']}/showed?t={_booking_token(b['id'], 'outcome_showed')}"
            no_url  = f"{base}/o/{b['id']}/noshow?t={_booking_token(b['id'], 'outcome_noshow')}"
            body = (f"Ραντεβού {b['time']} — {b['name']} ({b['service']}): ήρθε;\n"
                    f"Ναι: {yes_url}\nΌχι: {no_url}")
            sent = False
            owner_phone = (business.get("phone") or "").strip()
            if owner_phone:
                try:
                    from voice.twilio_client import send_sms as _tw_sms, is_configured as _tw_ok
                    if _tw_ok():
                        _tw_sms(owner_phone, body)
                        sent = True
                except Exception as sms_err:
                    print(f"[OUTCOME] owner SMS failed: {sms_err}")
            if not sent:
                try:
                    from email_service import send_owner_notification
                    send_owner_notification(b["slug"], {
                        "type": "no_show_detected",
                        "customer": b["name"], "date": b["date"], "time": b["time"],
                        "service": b["service"],
                        "detail": f"Ήρθε; Ναι: {yes_url} — Όχι: {no_url}",
                    })
                except Exception as own_err:
                    print(f"[OUTCOME] owner notify failed: {own_err}")
    except Exception as e:
        print(f"[OUTCOME SCHEDULER ERROR] {e}")


@app.route("/o/<int:booking_id>/<action>")
def booking_outcome_link(booking_id, action):
    """One-tap απάντηση ιδιοκτήτη: ήρθε (showed) / δεν ήρθε (noshow).
    Στο no-show → enqueue recovery SMS για το επόμενο πρωί στις 10:00."""
    token = request.args.get("t", "")
    if action not in ("showed", "noshow") or \
       not hmac.compare_digest(token, _booking_token(booking_id, f"outcome_{action}")):
        return "Άκυρος σύνδεσμος", 403
    outcome = "completed" if action == "showed" else "no_show"
    page = ("<html><body style='font-family:sans-serif;text-align:center;"
            "padding:60px 20px;'><h2>{msg}</h2></body></html>")
    if not mark_booking_outcome(booking_id, outcome):
        return page.format(msg="Έχει ήδη καταχωρηθεί ✔️")
    if outcome == "no_show":
        try:
            b = get_booking(booking_id)
            if b and b.get("phone"):
                now = datetime.now()
                nxt = now.replace(hour=10, minute=0, second=0, microsecond=0)
                if nxt <= now:
                    nxt += timedelta(days=1)
                enqueue_outbound_job(
                    slug=b["slug"], job_type="no_show_recovery",
                    customer_phone=b["phone"], customer_name=b.get("name", ""),
                    customer_email=b.get("email", ""), booking_id=booking_id,
                    scheduled_at=nxt.strftime("%Y-%m-%d %H:%M:%S"), channel="sms",
                    payload={"booking_date": b["date"], "booking_time": b["time"],
                             "service": b.get("service", ""),
                             "rebook_url": f"{_base_url()}/{b['slug']}"},
                )
        except Exception as e:
            print(f"[OUTCOME] recovery enqueue failed: {e}")
        return page.format(msg="Καταχωρήθηκε ως no-show. Το AI θα στείλει πρόταση "
                               "επανακράτησης αύριο το πρωί 📲")
    return page.format(msg="Τέλεια, καταχωρήθηκε ✔️")


def auto_infer_no_shows():
    """Outbound P2b: pending_outcome >20h χωρίς owner reply → auto no_show +
    recovery SMS + closed-loop waitlist fill. Μετατρέπει το recovery από
    'owner-tap-dependent' σε αυτόνομο = το defensible moat. Owner tap πάντα
    overrides μέσα στο 20h window (συντηρητικό, ~0 false positives)."""
    try:
        from database import get_stale_pending_outcomes
        base = _base_url()
        for b in get_stale_pending_outcomes(hours_since_end=20):
            if not mark_booking_outcome(b["id"], "no_show"):
                continue  # owner πρόλαβε / race — skip
            # 1) Recovery SMS (ίδιο pattern με το manual owner tap)
            if b.get("phone"):
                try:
                    now = datetime.now()
                    nxt = now.replace(hour=10, minute=0, second=0, microsecond=0)
                    if nxt <= now:
                        nxt += timedelta(days=1)
                    enqueue_outbound_job(
                        slug=b["slug"], job_type="no_show_recovery",
                        customer_phone=b["phone"], customer_name=b.get("name", ""),
                        customer_email=b.get("email", ""), booking_id=b["id"],
                        scheduled_at=nxt.strftime("%Y-%m-%d %H:%M:%S"), channel="sms",
                        payload={"booking_date": b["date"], "booking_time": b["time"],
                                 "service": b.get("service", ""),
                                 "rebook_url": f"{base}/{b['slug']}", "auto_inferred": True},
                    )
                except Exception as rec_err:
                    print(f"[AUTO-NOSHOW] recovery enqueue failed (booking {b['id']}): {rec_err}")
            # 2) Closed-loop: γέμισε το κενό slot από τη waitlist (same-day)
            try:
                notify_waitlist(b["slug"], b)
            except Exception as wl_err:
                print(f"[AUTO-NOSHOW] waitlist fill failed (booking {b['id']}): {wl_err}")
            print(f"[AUTO-NOSHOW] booking {b['id']} → auto no_show + recovery + waitlist")
    except Exception as e:
        print(f"[AUTO-NOSHOW SCHEDULER ERROR] {e}")


def send_review_requests():
    """Στέλνει review request σε bookings που τελείωσαν 2h πριν."""
    try:
        bookings = get_bookings_for_review(hours_after=2)
        for b in bookings:
            # Mark FIRST atomically
            if not mark_review_sent(b["id"]):
                continue
            business = get_business(b["slug"])
            if not business:
                continue
            send_review_request(b, business)
    except Exception as e:
        print(f"[REVIEW SCHEDULER ERROR] {e}")


def fill_empty_slots():
    try:
        base_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "http://localhost:5001")
        if not base_url.startswith("http"):
            base_url = "https://" + base_url
        for biz in list_businesses():
            slug     = biz["slug"]
            business = get_business(slug)
            if not business:
                continue
            empty_days = get_days_with_few_bookings(slug, days_ahead=3, threshold=2)
            if not empty_days:
                continue
            clients = get_clients_with_email(slug, limit=10)
            for client in clients:
                chat_url = f"{base_url}/{slug}"
                send_fill_slots_email(client, empty_days[0], chat_url, business)
                mark_outreach_sent(slug, client["phone"])
    except Exception as e:
        print(f"[FILL_SLOTS ERROR] {e}")


def reengage_inactive_clients():
    try:
        base_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "http://localhost:5001")
        if not base_url.startswith("http"):
            base_url = "https://" + base_url
        for biz in list_businesses():
            slug     = biz["slug"]
            business = get_business(slug)
            if not business:
                continue
            clients = get_clients_for_reengagement(slug, days=45, limit=10)
            for client in clients:
                full = get_client_profile(slug, client["phone"])
                if full:
                    client["history"] = full.get("history", [])
                chat_url = f"{base_url}/{slug}"
                send_reengagement_email(client, chat_url, business)
                mark_outreach_sent(slug, client["phone"])
    except Exception as e:
        print(f"[REENGAGE ERROR] {e}")


def weekly_db_backup():
    """Κάθε Κυριακή 03:00 — στέλνει DB snapshot στον master admin (off-site backup)."""
    try:
        import base64, sqlite3, urllib.request as _ureq
        from database import DB
        # online backup — safer than copying the file
        backup_path = DB + ".backup"
        src = sqlite3.connect(DB)
        dst = sqlite3.connect(backup_path)
        with dst:
            src.backup(dst)
        src.close(); dst.close()

        with open(backup_path, "rb") as _f:
            db_bytes = _f.read()
        try:
            os.remove(backup_path)
        except OSError:
            pass

        # GDPR: the DB holds special-category health data. NEVER email it in the clear.
        # Encrypt the whole snapshot with the Fernet key before it leaves the box; restore with
        # crypto_util.decrypt_bytes (needs RANTEVU_ENC_KEY). Recipient inbox only ever sees ciphertext.
        try:
            from crypto_util import encrypt_bytes
            db_bytes = encrypt_bytes(db_bytes)
            _enc_suffix, _enc_note = ".enc", " (κρυπτογραφημένο — restore με crypto_util.decrypt_bytes)"
        except Exception as _ce:
            print(f"[BACKUP] refusing to send UNENCRYPTED backup ({_ce.__class__.__name__}) — set RANTEVU_ENC_KEY")
            return

        owner_email = os.environ.get("BACKUP_EMAIL", "owner@example.com")
        resend_key  = os.environ.get("RESEND_API_KEY", "")
        if not resend_key:
            print("[BACKUP] RESEND_API_KEY not set — skipping")
            return

        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        payload = json.dumps({
            "from":    "Ran-Tevu Backup <noreply@ran-tevu.gr>",
            "to":      [owner_email],
            "subject": f"[Ran-Tevu] Weekly DB Backup — {ts}",
            "text":    f"Auto backup του bookings.db ({len(db_bytes)//1024} KB){_enc_note}.",
            "attachments": [{
                "filename": f"bookings_{ts}.db{_enc_suffix}",
                "content":  base64.b64encode(db_bytes).decode("ascii"),
            }],
        }).encode("utf-8")

        req = _ureq.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with _ureq.urlopen(req, timeout=30) as resp:
            print(f"[BACKUP] Sent ({len(db_bytes)//1024} KB) → {owner_email} | status={resp.status}")
    except Exception as e:
        print(f"[BACKUP ERROR] {e}")


def _detect_lead_language(email: str, name: str = "") -> str:
    """
    Returns 'el' (Greek) or 'en' (English) per lead.
    Rule: .gr TLD → Greek. Everything else → English.
    Also detects Greek characters in name as fallback for .com Greek businesses.
    """
    email_low = (email or "").lower().strip()
    if email_low.endswith(".gr"):
        return "el"
    # Fallback: if name has Greek characters, assume Greek
    if name:
        for ch in name:
            if "Ͱ" <= ch <= "Ͽ" or "ἀ" <= ch <= "῿":
                return "el"
    return "en"


def _cold_queue_daily():
    """Daily 09:00 — στέλνει personalized cold emails (Rigoudis) από το cold_queue.
    25/μέρα, MX-check, opt-out, skip already-sent/unsubscribed. 100% αυτόματο."""
    try:
        import cold_outreach
        res = cold_outreach.send_batch()
        print(f"[COLD QUEUE] {res}", flush=True)
    except Exception as e:
        print(f"[COLD QUEUE] error: {e}", flush=True)


def auto_cold_email_batch():
    """[DEPRECATED — replaced by _cold_queue_daily/cold_outreach] Generic 80/day from leads_master.csv.
    Auto-detects per-lead language (Greek for .gr, English for .com/.uk/etc).
    Skip already-sent (dedup μέσω email_sent_log). Resend free tier = 100/day."""
    try:
        import urllib.request as _ureq, urllib.error as _uerr

        resend_key = os.environ.get("RESEND_API_KEY", "")
        if not resend_key:
            return

        leads_path = os.path.join(BASE_DIR, "scraper", "leads_master.csv")
        if not os.path.exists(leads_path):
            print("[AUTO COLD] leads_master.csv missing")
            return

        import csv as _csv, re as _re
        leads = []
        seen_domain_name = set()  # Dedup: 1 email per (domain, name) combination
        with open(leads_path, encoding="utf-8-sig", errors="ignore") as _f:
            for row in _csv.DictReader(_f):
                email = (row.get("email") or "").strip()
                name  = (row.get("name")  or "").strip()
                # Validate email format (strict)
                if not email or "@" not in email:
                    continue
                if not _re.match(r"^[\w.+-]+@[\w-]+\.[\w.-]+$", email):
                    continue  # Skip malformed
                # Dedup: same domain + name = same person (permuted variants)
                domain = email.split("@", 1)[1].lower()
                dedup_key = (domain, (name or "").strip().lower())
                if dedup_key in seen_domain_name:
                    continue
                seen_domain_name.add(dedup_key)
                leads.append({
                    "email": email,
                    "name":  name,
                    "lang":  _detect_lead_language(email, name),
                })

        # Filter: not unsubscribed, not already sent
        eligible = [l for l in leads if not is_unsubscribed(l["email"]) and not was_sent(l["email"])]
        batch    = eligible[:80]  # Up to 80/day (Resend free tier = 100/day)

        if not batch:
            print("[AUTO COLD] no eligible leads remaining")
            return

        sent_count   = 0
        sent_el      = 0
        sent_en      = 0
        failed_count = 0
        quota_hit    = False

        demo_url_el = "https://www.ran-tevu.gr/beauty-demo"
        demo_url_en = "https://www.ran-tevu.gr/beauty-demo?lang=en"
        video_url   = "https://www.loom.com/share/a42066026aac476f80663667c84338cf"
        privacy_url = "https://www.ran-tevu.gr/compliance"
        sender_name = "Κωνσταντίνος"
        sender_name_en = "Konstantinos"

        for lead in batch:
            if quota_hit:
                break

            email_addr = lead["email"]
            lead_lang  = lead.get("lang", "el")
            unsub_token = _unsub_token(email_addr)
            unsub_url   = f"https://www.ran-tevu.gr/unsubscribe?email={email_addr}&t={unsub_token}"
            unsub_get   = f"https://www.ran-tevu.gr/api/unsubscribe?email={email_addr}&t={unsub_token}"

            if lead_lang == "en":
                # ── English template για ξένους leads (.com, .uk, .de, etc) ──
                subject = "24/7 AI receptionist for appointments — 14 days free"
                html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 16px"><tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.06)">
<tr><td style="background:#7C3AED;padding:24px 32px"><p style="margin:0;color:#fff;font-size:20px;font-weight:800">Ran-Tevu</p></td></tr>
<tr><td style="padding:30px 32px">
<p style="margin:0 0 14px;font-size:15px">Hello,</p>
<p style="margin:0 0 14px;font-size:15px;color:#374151;line-height:1.6">I'm {sender_name_en}, founder of Ran-Tevu. We built an AI receptionist that handles appointment requests 24/7 — when you can't reply on Instagram, WhatsApp, or your website.</p>
<p style="margin:0 0 14px;font-size:15px;color:#374151;line-height:1.6">It remembers returning customers, sends email confirmations, and is <strong>EU AI Act + GDPR compliant</strong>. Try it <strong>FREE for 14 days</strong>. After that €179/month flat. No commitment.</p>
<table cellpadding="0" cellspacing="0" style="margin:20px 0">
<tr><td style="background:#7C3AED;border-radius:10px"><a href="{demo_url_en}" style="display:block;padding:14px 28px;color:#fff;font-size:15px;font-weight:700;text-decoration:none">See live demo →</a></td></tr>
</table>
<p style="margin:12px 0;font-size:14px;color:#374151">📹 90-sec video walkthrough: <a href="{video_url}" style="color:#7C3AED">loom.com/...</a></p>
<p style="margin:12px 0 0;font-size:14px;color:#374151">If interested, reply here or contact me at <strong>+30 6900000000</strong>.</p>
</td></tr>
<tr><td style="background:#F9FAFB;padding:16px 32px;border-top:1px solid #E5E7EB">
<p style="margin:0;font-size:11px;color:#9CA3AF">Ran-Tevu · {sender_name_en} Gkioulbaxiotis · Rethymno, Greece · +30 6900000000<br>
<a href="{unsub_url}" style="color:#7C3AED">One-click unsubscribe</a> · <a href="{privacy_url}" style="color:#7C3AED">Privacy</a></p>
</td></tr>
</table></td></tr></table></body></html>"""
                text = (
                    f"Hello,\n\n"
                    f"I'm {sender_name_en}, founder of Ran-Tevu. We built an AI receptionist that handles "
                    f"appointment requests 24/7 — when you can't reply on Instagram, WhatsApp, or your website.\n\n"
                    f"It remembers returning customers, sends email confirmations, and is EU AI Act + GDPR compliant.\n\n"
                    f"Try it FREE for 14 days. After that €179/month flat. No commitment.\n\n"
                    f"Demo: {demo_url_en}\nVideo: {video_url}\n\n"
                    f"— {sender_name_en}\n"
                    f"+30 6900000000\n\n"
                    f"---\nUnsubscribe: {unsub_url}\n"
                )
                from_header = f"{sender_name_en} <noreply@ran-tevu.gr>"
            else:
                # ── Greek template (default for .gr) ──
                subject = "AI για ραντεβού 24/7 — 14 ημέρες δωρεάν"
                html = f"""<!DOCTYPE html><html lang="el"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 16px"><tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.06)">
<tr><td style="background:#7C3AED;padding:24px 32px"><p style="margin:0;color:#fff;font-size:20px;font-weight:800">Ran-Tevu</p></td></tr>
<tr><td style="padding:30px 32px">
<p style="margin:0 0 14px;font-size:15px">Καλησπέρα,</p>
<p style="margin:0 0 14px;font-size:15px;color:#374151;line-height:1.6">Είμαι ο {sender_name} από Ρέθυμνο. Έφτιαξα ένα AI που χειρίζεται μηνύματα ραντεβού 24/7 — όταν δεν προλαβαίνετε να απαντήσετε στο Instagram ή στο website σας.</p>
<p style="margin:0 0 14px;font-size:15px;color:#374151;line-height:1.6">Θυμάται κάθε επανερχόμενο πελάτη, στέλνει emails επιβεβαίωσης, και είναι <strong>EU AI Act + GDPR compliant</strong>. Δοκιμάστε ΔΩΡΕΑΝ 14 ημέρες. Μετά €179/μήνα flat. Καμία δέσμευση.</p>
<table cellpadding="0" cellspacing="0" style="margin:20px 0">
<tr><td style="background:#7C3AED;border-radius:10px"><a href="{demo_url_el}" style="display:block;padding:14px 28px;color:#fff;font-size:15px;font-weight:700;text-decoration:none">Δείτε live demo →</a></td></tr>
</table>
<p style="margin:12px 0;font-size:14px;color:#374151">📹 Video εξήγηση 90 sec: <a href="{video_url}" style="color:#7C3AED">loom.com/...</a></p>
<p style="margin:12px 0 0;font-size:14px;color:#374151">Αν σας ενδιαφέρει, απαντήστε εδώ ή καλέστε με στο <strong>6900000000</strong>.</p>
</td></tr>
<tr><td style="background:#F9FAFB;padding:16px 32px;border-top:1px solid #E5E7EB">
<p style="margin:0;font-size:11px;color:#9CA3AF">Ran-Tevu · Κωνσταντίνος Γκιουλμπαξιώτης · Ρέθυμνο · 6900000000<br>
<a href="{unsub_url}" style="color:#7C3AED">Διαγραφή με ένα κλικ</a> · <a href="{privacy_url}" style="color:#7C3AED">Πολιτική</a></p>
</td></tr>
</table></td></tr></table></body></html>"""
                text = (
                    f"Καλησπέρα,\n\n"
                    f"Είμαι ο {sender_name} από Ρέθυμνο. Έφτιαξα ένα AI που χειρίζεται μηνύματα "
                    f"ραντεβού 24/7 — όταν δεν προλαβαίνετε.\n\n"
                    f"Θυμάται κάθε επανερχόμενο πελάτη. EU AI Act + GDPR compliant.\n\n"
                    f"Δοκιμάστε ΔΩΡΕΑΝ 14 ημέρες. Μετά €179/μήνα flat. Καμία δέσμευση.\n\n"
                    f"Demo: {demo_url_el}\nVideo: {video_url}\n\n"
                    f"— {sender_name}\n"
                    f"📞 6900000000\n\n"
                    f"---\nΔιαγραφή: {unsub_url}\n"
                )
                from_header = "Κωνσταντίνος <noreply@ran-tevu.gr>"

            payload = json.dumps({
                "from":     from_header,
                "to":       [email_addr],
                "reply_to": "owner@example.com",
                "subject":  subject,
                "html":     html,
                "text":     text,
                "headers": {
                    "List-Unsubscribe":      f"<{unsub_get}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                    "Precedence":            "bulk",
                },
            }).encode("utf-8")

            req = _ureq.Request(
                "https://api.resend.com/emails",
                data=payload,
                headers={
                    "Authorization": f"Bearer {resend_key}",
                    "Content-Type":  "application/json",
                    "User-Agent":    "Mozilla/5.0",
                    "Accept":        "application/json",
                },
                method="POST",
            )
            try:
                with _ureq.urlopen(req, timeout=15) as resp:
                    if resp.status in (200, 201):
                        sent_count += 1
                        if lead_lang == "en":
                            sent_en += 1
                        else:
                            sent_el += 1
                        mark_sent(email_addr, campaign="cold", status="ok")
                    else:
                        failed_count += 1
            except _uerr.HTTPError as e:
                if e.code == 429:
                    quota_hit = True
                    print(f"[AUTO COLD] Quota hit at {sent_count} sent")
                    break
                failed_count += 1
                mark_sent(email_addr, campaign="cold", status=f"http_{e.code}")
            except Exception:
                failed_count += 1

            import time as _t
            _t.sleep(0.6)

        print(f"[AUTO COLD] sent={sent_count} (el={sent_el}, en={sent_en}), failed={failed_count}, quota_hit={quota_hit}")
    except Exception as e:
        print(f"[AUTO COLD ERROR] {e}")


def check_founder_reminders():
    """Κάθε μέρα 08:00 — ελέγχει reminders.json και στέλνει email αν κάποιο reminder είναι σε warn_days."""
    try:
        import urllib.request as _ureq
        from datetime import datetime, date

        rem_path = os.path.join(BASE_DIR, "reminders.json")
        if not os.path.exists(rem_path):
            return

        with open(rem_path, encoding='utf-8') as _f:
            data = json.load(_f)

        resend_key = os.environ.get("RESEND_API_KEY", "")
        if not resend_key:
            return

        today = date.today()
        sent_today = []

        for rem in data.get("reminders", []):
            expiry_str = rem.get("expiry_date", "")
            if not expiry_str or expiry_str == "TBD_USER_PROVIDES":
                continue
            try:
                expiry = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            except:
                continue

            days_until = (expiry - today).days
            warn_days = rem.get("warn_days", [2, 1])

            if days_until in warn_days:
                # Send reminder
                title = rem.get("title", "Reminder")
                desc = rem.get("description", "")
                url = rem.get("url", "")

                html = f"""<!DOCTYPE html><html><body style="font-family:-apple-system,sans-serif;background:#f9fafb;padding:30px">
<table width="600" align="center" style="background:#fff;border-radius:14px;padding:30px;box-shadow:0 4px 20px rgba(0,0,0,.08)">
<tr><td>
<h1 style="color:#DC2626;margin:0 0 10px;font-size:22px">⚠️ Reminder σε {days_until} {'ημέρα' if days_until == 1 else 'ημέρες'}</h1>
<p style="color:#6B7280;margin:0 0 20px">Λήγει: <b>{expiry_str}</b></p>

<h2 style="color:#7C3AED;font-size:18px">{title}</h2>
<p style="font-size:15px;color:#374151;line-height:1.6">{desc}</p>

{'<a href="' + url + '" style="display:inline-block;background:#7C3AED;color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:600;margin-top:14px">Action τώρα →</a>' if url else ''}

<p style="font-size:12px;color:#9CA3AF;margin-top:24px;border-top:1px solid #E5E7EB;padding-top:16px">Auto reminder · Ran-Tevu</p>
</td></tr>
</table>
</body></html>"""

                payload = json.dumps({
                    "from":    "Reminder <noreply@ran-tevu.gr>",
                    "to":      ["owner@example.com"],
                    "subject": f"⚠️ {title} — σε {days_until} {'ημέρα' if days_until == 1 else 'ημέρες'}",
                    "html":    html,
                }).encode("utf-8")

                try:
                    req = _ureq.Request(
                        "https://api.resend.com/emails",
                        data=payload,
                        headers={
                            "Authorization": f"Bearer {resend_key}",
                            "Content-Type":  "application/json",
                            "User-Agent":    "Mozilla/5.0",
                        },
                        method="POST",
                    )
                    with _ureq.urlopen(req, timeout=15) as resp:
                        sent_today.append(f"{rem['id']} ({days_until}d)")
                        print(f"[REMINDER] Sent: {title} ({days_until} days remaining)")
                except Exception as e:
                    print(f"[REMINDER ERROR] {rem['id']}: {e}")

        if sent_today:
            print(f"[REMINDERS] Sent {len(sent_today)}: {sent_today}")
    except Exception as e:
        print(f"[REMINDERS CHECK ERROR] {e}")


def send_founder_daily_report():
    """Κάθε μέρα 08:00 — στέλνει daily report στον founder (Konstantinos).
    Περιλαμβάνει: today's LinkedIn post, stats, hot leads, today's actions.
    Auto-rotates content from daily_content_bank.json based on day index.
    """
    try:
        import urllib.request as _ureq
        from datetime import datetime
        from database import count_sent, list_unsubscribes

        # Load content bank
        bank_path = os.path.join(BASE_DIR, "daily_content_bank.json")
        if not os.path.exists(bank_path):
            print("[DAILY REPORT] content bank missing")
            return

        with open(bank_path, encoding='utf-8') as _f:
            bank = json.load(_f)

        # Pick post by day index (cycling through 30)
        day_index = (datetime.now().day - 1) % len(bank["posts"])
        post = bank["posts"][day_index]

        # Get stats
        total_sent = count_sent("cold")
        unsubs = len(list_unsubscribes())

        # Get today's date Greek
        days = ['Δευτέρα','Τρίτη','Τετάρτη','Πέμπτη','Παρασκευή','Σάββατο','Κυριακή']
        today_str = f"{days[datetime.now().weekday()]} {datetime.now().strftime('%d/%m/%Y')}"

        # Build HTML email
        post_safe = post['content'].replace('\n', '<br>')
        html = f"""<!DOCTYPE html><html lang="el"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,sans-serif;color:#111;line-height:1.6">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:30px 16px"><tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)">
<tr><td style="background:#7C3AED;padding:28px 36px">
<h1 style="margin:0;color:#fff;font-size:22px;font-weight:800">📊 Daily — {today_str}</h1>
<p style="margin:6px 0 0;color:#DDD6FE;font-size:13px">Από Claude · Auto 08:00</p>
</td></tr>
<tr><td style="padding:28px 36px">

<h2 style="color:#7C3AED;font-size:17px;margin:0 0 12px">📝 Σήμερα LinkedIn Post #{post['day']} — {post['type']}</h2>
<div style="background:#F9FAFB;padding:16px;border-radius:10px;font-size:13px;font-family:Consolas,monospace;white-space:pre-wrap;color:#374151;line-height:1.6">{post_safe}</div>

<h2 style="color:#7C3AED;font-size:17px;margin:24px 0 12px">📊 Stats</h2>
<table width="100%" cellpadding="0" cellspacing="0" style="font-size:14px">
<tr><td style="padding:8px 0;border-bottom:1px solid #F3F4F6">📧 Total emails sent</td><td style="padding:8px 0;border-bottom:1px solid #F3F4F6;text-align:right"><b>{total_sent}</b></td></tr>
<tr><td style="padding:8px 0;border-bottom:1px solid #F3F4F6">🚫 Unsubscribes</td><td style="padding:8px 0;border-bottom:1px solid #F3F4F6;text-align:right"><b>{unsubs}</b></td></tr>
</table>

<h2 style="color:#7C3AED;font-size:17px;margin:24px 0 12px">🎯 Σημερινές actions</h2>
<ol style="font-size:14px;color:#374151">
<li><b>09:00</b> — Post LinkedIn (copy-paste από πάνω)</li>
<li><b>10:00</b> — Reply σε hot leads (Gmail)</li>
<li><b>13:00</b> — Demos αν booked</li>
<li><b>18:00</b> — Quick check Gmail + Cal.com bookings</li>
</ol>

<div style="background:#F0FDF4;border-left:4px solid #10B981;padding:14px 16px;border-radius:8px;margin-top:20px">
<p style="margin:0;font-size:13px;color:#065F46"><b>💡 Reminder:</b> Pricing είναι €179/μήνα (Standard) / €299 (Premium) + 14 ημέρες ΔΩΡΕΑΝ χωρίς κάρτα. Phone: 6900000000.</p>
</div>

</td></tr>
<tr><td style="background:#F9FAFB;padding:16px 36px;text-align:center;font-size:11px;color:#9CA3AF">
Auto-generated από Claude · Ran-Tevu · Κωνσταντίνος Γκιουλμπαξιώτης
</td></tr>
</table></td></tr></table></body></html>"""

        resend_key = os.environ.get("RESEND_API_KEY", "")
        if not resend_key:
            print("[DAILY REPORT] RESEND_API_KEY missing")
            return

        payload = json.dumps({
            "from":    "Claude (Daily) <noreply@ran-tevu.gr>",
            "to":      ["owner@example.com"],
            "reply_to": "owner@example.com",
            "subject": f"📊 Daily Report — {today_str}",
            "html":    html,
        }).encode("utf-8")

        req = _ureq.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type":  "application/json",
                "User-Agent":    "Mozilla/5.0",
            },
            method="POST",
        )
        with _ureq.urlopen(req, timeout=15) as resp:
            print(f"[DAILY REPORT] Sent OK ({resp.status}) — Day {post['day']} post")
    except Exception as e:
        print(f"[DAILY REPORT ERROR] {e}")


def send_weekly_reports():
    """Κάθε Δευτέρα 9:00 — στέλνει αναφορά στον ιδιοκτήτη κάθε ενεργής επιχείρησης."""
    try:
        from datetime import datetime, timedelta
        today      = datetime.now().date()
        week_end   = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        week_start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        for biz in list_businesses():
            slug        = biz["slug"]
            owner_email = biz.get("owner_email", "")
            if not owner_email:
                continue
            business = get_business(slug)
            if not business:
                continue
            stats = get_weekly_stats(slug, week_start, week_end)
            send_weekly_report(owner_email=owner_email, business=business, stats=stats)
    except Exception as e:
        print(f"[WEEKLY REPORTS ERROR] {e}")


def daily_back_office_refresh():
    """Αυτόνομο daily (07:30): για κάθε ιατρείο τρέχει τους back-office agents —
    sync ασθενών από ραντεβού + auto-τιμολόγια ολοκληρωμένων + ανανέωση «τι να κάνω σήμερα».
    Έτσι όταν ανοίγει ο γιατρός το chat, η εικόνα/εκκρεμότητες είναι ΗΔΗ φρέσκες. Μηδέν effort."""
    try:
        import back_office as _bo
        n = 0
        for b in list_businesses():
            slug = b.get("slug") if isinstance(b, dict) else b["slug"]
            try:
                _bo.run_all_agents(slug)
                n += 1
            except Exception as e:
                print(f"[BO-REFRESH] {slug} failed: {e}")
        if n:
            print(f"[BO-REFRESH] refreshed back-office for {n} businesses")
    except Exception as e:
        print(f"[BO-REFRESH ERROR] {e}")


def _daily_briefing_send():
    """Αυτόνομο daily (07:45): ο AI ΣΤΕΛΝΕΙ στον γιατρό σύντομη ενημέρωση (τι έχει σήμερα,
    πόσα ανέκτησε, χρωστούμενα, εκκρεμότητες) — γραπτό (SMS→email) ή φωνητικό ανά ιατρείο.
    Τρέχει μετά το data refresh ώστε τα νούμερα να είναι φρέσκα. Μηδέν effort για τον γιατρό."""
    try:
        import briefing
        stats = briefing.run_daily()
        print(f"[BRIEFING] {stats}")
    except Exception as e:
        print(f"[BRIEFING ERROR] {e}")


def winback_all_businesses():
    """Outbound P4: weekly win-back ανενεργών πελατών (90d) για ΟΛΑ τα businesses.
    Ξεκλειδώνει το dark win-back feature — έτρεχε μόνο manual (/outbound/winback).
    Cooldown 60d μέσα στο enqueue ώστε να μη σπαμάρει τον ίδιο ασθενή."""
    try:
        from voice.outbound import enqueue_winback_for_inactive_customers
        try:
            from vertical_templates import winback_days as _wb_days
        except Exception:
            _wb_days = lambda t: 90
        total = 0
        for b in list_businesses():
            slug = b.get("slug") if isinstance(b, dict) else b["slug"]
            btype = (b.get("type", "") if isinstance(b, dict) else "") or ""
            days = _wb_days(btype)  # vertical-specific: physio 14, medical 180 (6μηνο recall), fitness 60
            try:
                total += enqueue_winback_for_inactive_customers(slug, days_inactive=days, limit=20)
            except Exception as e:
                print(f"[WINBACK] {slug} failed: {e}")
        if total:
            print(f"[WINBACK] enqueued {total} win-back jobs across all businesses")
    except Exception as e:
        print(f"[WINBACK SCHEDULER ERROR] {e}")


def payment_reminders_all():
    """Daily: αυτόματη ευγενική υπενθύμιση πληρωμής σε χρωστούμενους, ανά ιατρείο
    (dedup 7 ημερών). Έτσι το follow-up πληρωμών τρέχει μόνο του."""
    try:
        import back_office as _bo
        from database import list_businesses
        total = 0
        for b in list_businesses():
            try:
                total += _bo.auto_payment_reminders(b["slug"]).get("reminded", 0)
            except Exception as e:
                print(f"[PAYMENT-REMINDER] {b['slug'] if b else '?'}: {e}")
        print(f"[PAYMENT-REMINDER] sent {total}", flush=True)
    except Exception as e:
        print(f"[PAYMENT-REMINDER] failed: {e}")


from apscheduler.schedulers.background import BackgroundScheduler
_scheduler = BackgroundScheduler(daemon=True)


def _scheduler_status():
    jobs = []
    try:
        for job in _scheduler.get_jobs():
            jobs.append({
                "id": getattr(job, "id", None),
                "name": getattr(job, "name", None),
                "trigger": str(getattr(job, "trigger", "")),
                "next_run_time": getattr(job, "next_run_time", None).isoformat() if getattr(job, "next_run_time", None) else None,
            })
    except Exception:
        jobs = []
    return jobs


def _register_core_scheduler_jobs():
    _scheduler.add_job(check_reminders,          'interval', minutes=30)
    _scheduler.add_job(check_risk_reminders,     'interval', hours=1)   # risk_brain: έξτρα υπενθύμιση στους high-risk (~48h πριν)
    _scheduler.add_job(send_questionnaires,      'interval', minutes=30)
    _scheduler.add_job(send_review_requests,     'interval', minutes=30)
    _scheduler.add_job(fill_empty_slots,         'cron', hour=10, minute=0)
    _scheduler.add_job(reengage_inactive_clients,'cron', day_of_week='mon', hour=11, minute=0)
    _scheduler.add_job(winback_all_businesses,   'cron', day_of_week='tue', hour=11, minute=30)  # P4: weekly win-back (was dark/manual-only)
    _scheduler.add_job(payment_reminders_all,    'cron', hour=11, minute=15)  # daily: αυτόματες υπενθυμίσεις πληρωμών (follow-up χρωστούμενων)
    _scheduler.add_job(daily_back_office_refresh,'cron', hour=7, minute=30)  # daily: fresh tasks/invoices/patients ανά ιατρείο
    _scheduler.add_job(_daily_briefing_send,     'cron', hour=7, minute=45)  # daily: ο AI ΣΤΕΛΝΕΙ ενημέρωση στον γιατρό (μετά το refresh)
    _scheduler.add_job(send_weekly_reports,      'cron', day_of_week='mon', hour=9, minute=0)
    try:
        from value_report import send_value_reports_all as _send_value_reports
        _scheduler.add_job(_send_value_reports,  'cron', day_of_week='mon', hour=9, minute=20, id='proof_of_value')
        print("[VALUE REPORT] proof-of-value weekly report registered (Mon 09:20)")
    except Exception as _e:
        print(f"[VALUE REPORT] scheduler registration failed: {_e}")
    _scheduler.add_job(weekly_db_backup,         'cron', day_of_week='sun', hour=3, minute=0)
    _scheduler.add_job(send_founder_daily_report,'cron', hour=8, minute=0)  # Daily 08:00 — founder report
    _scheduler.add_job(check_founder_reminders, 'cron', hour=8, minute=5)  # Daily 08:05 — reminders check
    _scheduler.add_job(_cold_queue_daily,        'cron', hour=9, minute=0)  # Daily 09:00 — personalized cold emails από queue (Rigoudis, 25/day)


# Feature #3 (Outbound Proactive AI) — process due outbound jobs every 5 minutes
try:
    from voice.outbound import process_due_jobs as _process_outbound
    _scheduler.add_job(_process_outbound, 'interval', minutes=5, id='outbound_proactive')
    print("[OUTBOUND] proactive AI scheduler registered (every 5 min)")
except Exception as _e:
    print(f"[OUTBOUND] scheduler integration failed: {_e}")

# Outbound P2 — no-show detection: ραντεβού χωρίς αποτέλεσμα → one-tap SMS στον ιδιοκτήτη
try:
    _scheduler.add_job(check_booking_outcomes, 'interval', minutes=30, id='booking_outcomes')
    print("[OUTCOME] no-show detection registered (every 30 min)")
except Exception as _e:
    print(f"[OUTCOME] scheduler registration failed: {_e}")

# Outbound P2b — AUTO no-show inference: pending_outcome >20h χωρίς owner reply →
# auto no_show + recovery + waitlist fill (το defensible moat, αυτόνομο recovery)
try:
    _scheduler.add_job(auto_infer_no_shows, 'interval', hours=2, id='auto_no_show')
    print("[AUTO-NOSHOW] auto no-show inference registered (every 2h, 20h window)")
except Exception as _e:
    print(f"[AUTO-NOSHOW] scheduler registration failed: {_e}")

# Autonomous LEARNING loop — server-side, χωρίς prompt: παρατηρεί νέα outcomes → μαθαίνει το
# no-show μοντέλο (κινείται προς τον αληθινό ρυθμό) → εκτελεί ανάκτηση (idempotent, consent-gated).
try:
    from autonomous_loop import run_all_clinics as _autoloop
    _scheduler.add_job(_autoloop, 'interval', hours=1, id='autonomous_loop')
    print("[AUTOLOOP] autonomous learning loop registered (every 1h, server-side)")
except Exception as _e:
    print(f"[AUTOLOOP] scheduler registration failed: {_e}")

# Claude Agent SDK autonomous agents (2026-05-29)
# Off by default — set AGENTS_V2_ENABLED=1 in Railway env to activate.
# Set AGENTS_V2_LIVE_SENDS=1 and AGENTS_V2_LIVE_REPLIES=1 ONLY when ready for real sends.
if os.environ.get("AGENTS_V2_ENABLED", "0") == "1":
    try:
        from agents_v2.orchestrator import register_scheduled_jobs as _register_v2
        _v2_ids = _register_v2(_scheduler)
        print(f"[AGENTS_V2] autonomous agents registered: {_v2_ids}")
    except Exception as _e:
        print(f"[AGENTS_V2] not loaded: {_e}")
else:
    print("[AGENTS_V2] disabled (set AGENTS_V2_ENABLED=1 to activate)")

# ── AutoSales scheduler integration (5 jobs: counter reset / scrape / email / drips / replies) ──
# ── Autonomous agents (today / money / orchestrator / reality / replies) ──
try:
    from agents import register_agent_jobs as _register_agents
    _agent_job_ids = _register_agents(_scheduler)
    print(f"[AGENTS] scheduler jobs registered: {_agent_job_ids}")
except Exception as _e:
    print(f"[AGENTS] scheduler integration failed: {_e}")


# Start once, only if not already running
try:
    if not _scheduler.running:
        _scheduler.start()
    print(f"[SCHEDULER] started with {_scheduler_status().__len__()} jobs")
except Exception as _e:
    print(f"[SCHEDULER] start failed: {_e}")

print("[SCHEDULER] Started — reminders/30min | questionnaires/30min | reviews/30min | fill_slots/10:00 | reengage/Mon 11:00 | weekly_report/Mon 09:00 | backup/Sun 03:00 | daily_founder/08:00")


# ════════════════════════════════════════════════════════════════════
#  AI LOGIC
# ════════════════════════════════════════════════════════════════════

def build_system_prompt(business: dict, client_profile: dict = None, lang: str = "el",
                        memory: dict = None) -> str:
    en_names = business.get("en_service_names", {})

    def _en(gr_name):
        """Return English name if available and lang=en, else Greek name."""
        if lang == "en" and en_names:
            for k, v in en_names.items():
                import unicodedata
                def _n(s): return unicodedata.normalize('NFD', s).encode('ascii','ignore').decode().lower()
                if _n(k) == _n(gr_name):
                    return v
        return gr_name

    if lang == "en":
        services_text = "\n".join(
            f"  • {_en(s['name'])} — {s['duration']} min — {s['price']}€  [tool name: \"{s['name']}\"]"
            for s in business["services"]
        )
    else:
        services_text = "\n".join(
            f"  • {s['name']} — {s['duration']} λεπτά — {s['price']}€"
            for s in business["services"]
        )
    upsells_text = "\n".join(
        f"  • Trigger: '{u['trigger']}' → Πες: \"{u['message']}\""
        for u in business.get("upsells", [])
    )
    hours_text = "\n".join(
        f"  {day}: {hours}" for day, hours in business["hours"].items()
    )
    now       = datetime.now()
    today_str = now.strftime("%A %d/%m/%Y")
    tomorrow  = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    today_iso = now.strftime("%Y-%m-%d")

    memory_section = ""
    if client_profile and client_profile.get("name"):
        visits = client_profile.get("visit_count", 0)
        history_lines = ""
        if client_profile.get("history"):
            history_lines = "\nΙστορικό:\n" + "\n".join(
                f"  • {h['service']} ({h['date']})" for h in client_profile["history"][:5]
            )
        loyalty_note = ""
        if visits == 5:
            loyalty_note = "\n→ Ενημέρωσέ τον/την: 'Είσαι VIP πελάτης μας με 5 επισκέψεις! 🌟'"
        elif visits >= 10:
            loyalty_note = "\n→ Ενημέρωσέ τον/την ότι αξίζει έκπτωση και να επικοινωνήσει μαζί μας."
        memory_section = f"""
━━━ ΕΠΙΣΤΡΟΦΩΝ ΠΕΛΑΤΗΣ ━━━
Όνομα: {client_profile.get('name')}
Επισκέψεις: {visits}
Τελευταία: {client_profile.get('last_visit', '—')}
Προτιμήσεις: {client_profile.get('preferences', '—')}
Σημειώσεις: {client_profile.get('skin_notes', '—')}{history_lines}

→ Χαιρέτησέ τον/την με το μικρό όνομα.
→ Αν έχει ιστορικό, πρότεινε την υπηρεσία που κάνει συνήθως.
→ Αν έχει πάνω από 3 επισκέψεις, αναφέρσου σε αυτό φιλικά.{loyalty_note}
━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

    # ── AI Memory (wave: long-term memory layer) ─────────────────────────────
    ai_memory_section = render_memory_section(memory, lang) if memory else ""

    # ── Personality layer (wave: AI companions / warmth) ─────────────────────
    if lang == "en":
        personality_section = """
━━━ PERSONALITY (be human, not robot) ━━━
- Speak warmly, like a friendly local who knows the customer.
- Use natural phrasing — contractions, light enthusiasm ("great!", "perfect").
- AVOID corporate phrases like "I will assist you" / "How may I help".
- 1 short message at a time. No walls of text.
- If the customer seems stressed/upset, acknowledge briefly ("I hear you") before solving.
- Remember earlier details in THIS conversation (their name, their problem).
- Occasionally (NOT every time) ask a light personal question if it fits — e.g., on a return visit "how did the last one go?"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    else:
        personality_section = """
━━━ ΠΡΟΣΩΠΙΚΟΤΗΤΑ (να είσαι άνθρωπος, όχι ρομπότ) ━━━
- Μίλα ζεστά, σαν φιλική/ος γνωστή/ος της γειτονιάς που ξέρει τον πελάτη.
- Φυσική γλώσσα — λέξεις όπως "έγινε!", "τέλεια", "μην ανησυχείς".
- ΑΠΟΦΕΥΓΕ ψυχρές φράσεις: "Πώς μπορώ να σας βοηθήσω;", "Παρακαλώ αναμείνατε".
- 1 σύντομο μήνυμα κάθε φορά. Όχι τοίχους κειμένου.
- Αν ο πελάτης φαίνεται στρεσαρισμένος/εκνευρισμένος, αναγνώρισέ το σύντομα ("σε καταλαβαίνω") πριν λύσεις.
- Θυμήσου λεπτομέρειες ΜΕΣΑ σε αυτή τη συνομιλία (όνομα, πρόβλημα).
- Που και που (ΟΧΙ κάθε φορά) ρώτα κάτι προσωπικό αν ταιριάζει — π.χ. σε επανερχόμενο πελάτη "πώς πήγε το προηγούμενο;".
- Αν ο πελάτης χρησιμοποιεί διάλεκτο/αργκό, ΜΕΙΝΕ στην ίδια φυσική απόχρωση.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

    upsells_section = ""
    if upsells_text:
        upsells_section = f"""
UPSELLING (μόνο μία φορά, μόνο αν ταιριάζει):
{upsells_text}
ΚΑΝΟΝΕΣ UPSELLING:
- Πρότεινε ΜΟΝΟ μία φορά, αμέσως μόλις επιλεγεί η υπηρεσία
- Αν ο πελάτης αρνηθεί ή πει "όχι" → ΣΥΝΕΧΙΣΕ αμέσως στο βήμα 3
- Αν πει "ναι" ή "επιβεβαιώνω" → book_appointment αμέσως
"""

    lang_instruction = (
        "LANGUAGE: Respond exclusively in English. Use the English service names shown above when talking to the customer. "
        "When calling tools (book_appointment, get_slots etc.) always use the exact name shown in [tool name: \"...\"]."
        if lang == "en"
        else "ΓΛΩΣΣΑ: Απαντάς αποκλειστικά στα Ελληνικά."
    )

    # ── Type-specific language & flow ─────────────────────────────────────────
    btype = business.get("type", "").lower()

    if btype == "restaurant":
        booking_word   = "κράτηση"
        booking_action = "ΚΛΕΙΣΙΜΟ ΚΡΑΤΗΣΗΣ"
        cancel_word    = "κράτηση"
        service_label  = "τραπέζι (αριθμός ατόμων)"
        services_label = "ΕΠΙΛΟΓΕΣ ΤΡΑΠΕΖΙΟΥ"
        step1          = "1. Αριθμός ατόμων: ρώτα πόσα άτομα είναι"
        extra_steps    = "2. Αφορμή (προαιρετικά): ρώτα αν είναι για κάποια ειδική περίπτωση (γενέθλια, επέτειος κλπ)"
        role_desc      = "διαχειρίζεσαι κρατήσεις τραπεζιών"
        cancel_msg     = "Ποια κράτηση να ακυρώσω;"
    elif btype in ("pilates", "yoga", "fitness", "gym"):
        booking_word   = "θέση / session"
        booking_action = "ΚΛΕΙΣΙΜΟ ΘΕΣΗΣ"
        cancel_word    = "θέση"
        service_label  = "μάθημα ή session"
        services_label = "ΜΑΘΗΜΑΤΑ & SESSIONS"
        step1          = "1. Μάθημα/Session: αν δεν έχει πει → ρώτα"
        extra_steps    = ""
        role_desc      = "διαχειρίζεσαι κρατήσεις θέσεων σε μαθήματα και sessions"
        cancel_msg     = "Ποια κράτηση να ακυρώσω;"
    else:
        # Default: hair, beauty, nails, spa, medical, vet κλπ
        booking_word   = "ραντεβού"
        booking_action = "ΚΛΕΙΣΙΜΟ ΡΑΝΤΕΒΟΥ"
        cancel_word    = "ραντεβού"
        service_label  = "υπηρεσία"
        services_label = "ΥΠΗΡΕΣΙΕΣ & ΤΙΜΕΣ"
        step1          = "1. Υπηρεσία: αν δεν έχει πει → ρώτα"
        extra_steps    = ""
        role_desc      = "κλείνεις ραντεβού"
        cancel_msg     = "Ποιο ραντεβού να ακυρώσω;"

    # ── Medical-safety guardrails for health verticals (chat front desk) ──
    _bt = (business.get('type') or '').lower()
    _health = any(k in _bt for k in (
        'ιατρ', 'γιατρ', 'κλινικ', 'clinic', 'medical', 'doctor', 'φυσιοθερ', 'φυσικοθερ',
        'physio', 'οδοντ', 'dental', 'κτηνιατ', 'vet', 'ψυχολ', 'ψυχιατρ', 'διαιτ', 'παιδίατ',
        'δερματ', 'καρδιολ', 'παθολ', 'γυναικολ', 'ορθοπ', 'νευρολ', 'ουρολ', 'οφθαλμ',
        'λογοθερ', 'health', 'υγεί'))
    medical_safety_section = ""
    if _health:
        medical_safety_section = (
            "\n━━━ 🚨 ΙΑΤΡΙΚΗ ΑΣΦΑΛΕΙΑ (ΥΨΗΛΗ ΠΡΟΤΕΡΑΙΟΤΗΤΑ) ━━━\n"
            "- ΚΑΜΙΑ ιατρική συμβουλή/διάγνωση/φαρμακευτική οδηγία. Αν περιγράφει συμπτώματα → «αυτό θα το δει ο γιατρός, ας κλείσουμε ραντεβού».\n"
            "- ΕΠΕΙΓΟΝ απειλητικό για τη ζωή (πόνος στο στήθος, δύσπνοια, λιποθυμία, βαριά αιμορραγία, σημάδια εγκεφαλικού, σοβαρή αλλεργία, σκέψεις αυτοκτονίας): πες ΑΜΕΣΩΣ «Αυτό είναι επείγον — καλέστε τώρα το 166 ή το 112, ή πηγαίνετε στα επείγοντα» και ΜΗΝ κλείνεις ραντεβού, ΜΗΝ κάνεις triage.\n"
            "- ΠΟΤΕ μην εφευρίσκεις τιμές/ωράρια/υπηρεσίες/ασφαλιστικά — αν δεν το ξέρεις, πες ότι θα ενημερώσει ο γιατρός.\n"
            "- Διακριτικότητα με ευαίσθητα δεδομένα υγείας.\n"
        )

    return f"""Είσαι η ψηφιακή γραμματεία του **{business['name']}** ({business.get('type','επιχείρηση')}) — {role_desc}.

{lang_instruction}

ΧΑΡΑΚΤΗΡΑΣ:
Μιλάς φυσικά και ζεστά, σαν έμπειρη ανθρώπινη γραμματεία. Είσαι αποτελεσματική. Χρησιμοποιείς μικρά emoji με μέτρο. Ποτέ δεν αναφέρεις ότι είσαι AI.
ΣΗΜΑΝΤΙΚΟ: Μην εμφανίζεις εσωτερικές σκέψεις ή οδηγίες. Μόνο η τελική απάντηση.

Σήμερα: {today_str} | Αύριο: {tomorrow}
{personality_section}
{ai_memory_section}
{memory_section}
━━━ ΣΤΟΙΧΕΙΑ ΕΠΙΧΕΙΡΗΣΗΣ ━━━
Διεύθυνση: {business['address']}
Τηλέφωνο:  {business['phone']}

ΩΡΑΡΙΟ:
{hours_text}

{services_label}:
{services_text}
{upsells_section}
━━━ ΔΙΑΔΙΚΑΣΙΕΣ ━━━

{booking_action} (ακολούθα αυτή ακριβώς τη σειρά):
{step1}
{extra_steps}
2. Upsell (αν υπάρχει): πρότεινε μία φορά
3. Ημερομηνία: ρώτα ποια μέρα
4. Ώρα: get_slots → παρουσίασε ομαδοποιημένα (πρωί/απόγευμα/βράδυ)
5. Στοιχεία: ρώτα όνομα, τηλέφωνο, email (σε ΕΝΑ μήνυμα)
6. Ανακεφαλαίωση: παρουσίασε όλα, ζήτα επιβεβαίωση
7. Αν επιβεβαιώσει: book_appointment αμέσως
8. Ενημέρωσε ότι στάλθηκε email επιβεβαίωσης

ΑΚΥΡΩΣΗ:
1. Ζήτα τηλέφωνο αν δεν το ξέρεις
2. cancel_appointment(phone=...) — ΜΗΝ πεις "ακυρώθηκε" αν success=false
3. Αν επιστρέψει needs_clarification=true → ρώτα "{cancel_msg}" και δείξε τη λίστα
4. Επιβεβαίωσε ακύρωση ΜΟΝΟ αν success=true

ΑΛΛΑΓΗ {booking_word.upper()}:
1. Ζήτα τηλέφωνο αν δεν το ξέρεις
2. find_my_bookings(phone=...) → πάρε το booking_id
3. Ζήτα νέα ημερομηνία/ώρα, έλεγξε διαθεσιμότητα με get_slots
4. reschedule_booking(booking_id=..., phone=..., new_date=..., new_time=...)
5. ΜΗΝ χρησιμοποιείς book_appointment για αλλαγή — πάντα reschedule_booking

━━━ ΚΑΝΟΝΕΣ ━━━
- Τιμές/{service_label}/ωράριο → απάντα ΑΜΕΣΑ, χωρίς tool
- Αν δεν υπάρχουν ώρες → προτείνε άλλη ημερομηνία
- Παράπονα → ακούς, συμπαθείς, προσφέρεις λύση
- Ποτέ μην κάνεις booking χωρίς ρητή επιβεβαίωση
- Αν ώρα είναι κατειλημμένη → πρότεινε waitlist
{medical_safety_section}
━━━ ΜΟΡΦΟΠΟΙΗΣΗ ━━━
- Bullet points (• ή -), ΟΧΙ markdown tables
- **bold** για έμφαση
- Συνοπτικές αλλά πλήρεις απαντήσεις
"""


def _build_tools(slug: str) -> list:
    return [
        {
            "name": "get_slots",
            "description": "Επιστρέφει διαθέσιμες ώρες για υπηρεσία/ημερομηνία.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date":    {"type": "string", "description": "YYYY-MM-DD"},
                    "service": {"type": "string"},
                },
                "required": ["date", "service"]
            }
        },
        {
            "name": "book_appointment",
            "description": "Κλείνει ραντεβού αφού επιβεβαιωθούν όλα τα στοιχεία.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date":    {"type": "string", "description": "YYYY-MM-DD"},
                    "time":    {"type": "string", "description": "HH:MM"},
                    "service": {"type": "string"},
                    "name":    {"type": "string"},
                    "phone":   {"type": "string"},
                    "email":   {"type": "string"},
                    "notes":   {"type": "string"},
                },
                "required": ["date", "time", "service", "name", "phone", "email"]
            }
        },
        {
            "name": "save_client_notes",
            "description": "Αποθηκεύει προτιμήσεις πελάτη.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "phone":       {"type": "string"},
                    "preferences": {"type": "string"},
                    "skin_notes":  {"type": "string"},
                },
                "required": ["phone"]
            }
        },
        {
            "name": "find_my_bookings",
            "description": "Βρίσκει επερχόμενα ραντεβού πελάτη.",
            "input_schema": {
                "type": "object",
                "properties": {"phone": {"type": "string"}},
                "required": ["phone"]
            }
        },
        {
            "name": "reschedule_booking",
            "description": "Αλλάζει ημερομηνία/ώρα ραντεβού. Χρησιμοποίησε booking_id από find_my_bookings.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "booking_id": {"type": "integer", "description": "Το id από find_my_bookings"},
                    "phone":      {"type": "string"},
                    "new_date":   {"type": "string", "description": "YYYY-MM-DD"},
                    "new_time":   {"type": "string", "description": "HH:MM"},
                },
                "required": ["booking_id", "phone", "new_date", "new_time"]
            }
        },
        {
            "name": "lookup_client",
            "description": "Αναζητά επιστρέφοντα πελάτη με τηλέφωνο.",
            "input_schema": {
                "type": "object",
                "properties": {"phone": {"type": "string"}},
                "required": ["phone"]
            }
        },
        {
            "name": "cancel_appointment",
            "description": "Ακυρώνει ραντεβού.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "phone":      {"type": "string"},
                    "booking_id": {"type": "integer"},
                },
                "required": ["phone"]
            }
        },
        {
            "name": "add_to_waitlist",
            "description": "Προσθέτει πελάτη στη λίστα αναμονής.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date":    {"type": "string"},
                    "time":    {"type": "string"},
                    "service": {"type": "string"},
                    "name":    {"type": "string"},
                    "phone":   {"type": "string"},
                    "email":   {"type": "string"},
                },
                "required": ["date", "time", "service", "name", "phone"]
            }
        },
    ]


def _find_service(business: dict, service_name: str):
    import unicodedata
    def _norm(s): return unicodedata.normalize('NFD', s).encode('ascii','ignore').decode().lower().strip()

    name_lower = service_name.lower().strip()
    name_norm  = _norm(service_name)

    # 1. Exact match on Greek name
    for s in business["services"]:
        if s["name"].lower() == name_lower:
            return s
    # 2. Partial match on Greek name
    for s in business["services"]:
        if s["name"].lower() in name_lower or name_lower in s["name"].lower():
            return s
    # 3. Match against English names (en_service_names)
    en_names = business.get("en_service_names", {})
    for gr_name, en_name in en_names.items():
        en_norm = _norm(en_name)
        if en_norm == name_norm or en_norm in name_norm or name_norm in en_norm:
            for s in business["services"]:
                if _norm(s["name"]) == _norm(gr_name):
                    return s
    return None


def _claim_token(claim_id: int) -> str:
    """HMAC token για waitlist auto-claim links."""
    return hmac.new(_signing_secret(), f"claim:{claim_id}".encode(), hashlib.sha256).hexdigest()[:24]


def notify_waitlist(slug: str, cancelled_booking: dict):
    try:
        waiter = pop_first_waiter(slug, cancelled_booking["date"], cancelled_booking["time"])
        if not waiter:
            return
        business = get_business(slug)

        # Δημιουργούμε pending_claim για ασφαλή επαναφορά αν χαθεί email/race
        claim_id = add_pending_claim(
            slug=slug,
            date=waiter["date"],
            time=waiter["time"],
            service=waiter["service"],
            name=waiter["name"],
            phone=waiter["phone"],
            email=waiter.get("email", ""),
            expires_in_hours=6,
        )

        # 1. Email στον waiter με one-click "πάρε εσύ τη θέση" link
        if business and waiter.get("email"):
            base = _base_url()
            token = _claim_token(claim_id)
            claim_url = f"{base}/claim/{slug}/{claim_id}?t={token}"
            send_waitlist_offer(waiter, claim_url, business)

        # 2. SMS στον waiter (αν SMS διαθέσιμο)
        msg = (f"Καλά νέα {waiter['name']}! Ελευθερώθηκε θέση για "
               f"{waiter['service']} στις {waiter['date']} {waiter['time']}. "
               f"Δες το email σου για να την πάρεις.")
        send_sms(waiter["phone"], msg)

        # 2. Email στον ιδιοκτήτη (πάντα, αν SMTP ρυθμισμένο)
        if business and business.get("smtp_email") and business.get("owner_email"):
            from email_service import _send_html
            subject = f"📋 Waitlist: {waiter['name']} περιμένει θέση — {waiter['date']} {waiter['time']}"
            html = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="utf-8"/></head>
<body style="margin:0;padding:24px;background:#f3f4f8;font-family:-apple-system,sans-serif;">
<div style="max-width:480px;background:#fff;border-radius:18px;padding:28px;box-shadow:0 4px 24px rgba(0,0,0,0.08);">
  <p style="margin:0 0 16px;font-size:16px;font-weight:700;color:#111827;">📋 Ελεύθερη θέση από waitlist</p>
  <p style="font-size:14px;color:#374151;margin:0 0 20px;">Ελευθερώθηκε θέση που περίμενε πελάτης. Επικοινώνησε μαζί του για να κλείσει.</p>
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:13px;color:#9ca3af;width:110px;">👤 Πελάτης</td>
        <td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:14px;font-weight:600;">{waiter['name']}</td></tr>
    <tr><td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:13px;color:#9ca3af;">📞 Τηλέφωνο</td>
        <td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:14px;">{waiter['phone']}</td></tr>
    <tr><td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:13px;color:#9ca3af;">💆 Υπηρεσία</td>
        <td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:14px;">{waiter['service']}</td></tr>
    <tr><td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:13px;color:#9ca3af;">📅 Ημερομηνία</td>
        <td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:14px;font-weight:700;color:#e91e8c;">{waiter['date']}</td></tr>
    <tr><td style="padding:9px 0;font-size:13px;color:#9ca3af;">🕐 Ώρα</td>
        <td style="padding:9px 0;font-size:14px;font-weight:700;color:#e91e8c;">{waiter['time']}</td></tr>
  </table>
  <a href="tel:{waiter['phone']}" style="display:inline-block;margin-top:20px;background:linear-gradient(135deg,#e91e8c,#c2185b);color:#fff;text-decoration:none;padding:12px 22px;border-radius:24px;font-size:13px;font-weight:600;">📞 Κάλεσε τώρα</a>
</div>
</body></html>"""
            _send_html(business=business, to=business["owner_email"], subject=subject, html=html)
            print(f"[WAITLIST ✅] Email → {business['owner_email']} για {waiter['name']}")
    except Exception as e:
        print(f"[WAITLIST ERROR] {e}")


def _get_day_hours(business: dict, target_date: str):
    """Returns (day_start, day_end, is_closed) using per-day hours if available."""
    import unicodedata
    from datetime import datetime as _dt
    def _nrm(s):
        # Strip combining accent marks (works for Greek + Latin)
        nfd = unicodedata.normalize('NFD', s)
        return ''.join(c for c in nfd if unicodedata.category(c) != 'Mn').lower().strip()
    _DAY_GR = ["Δευτέρα","Τρίτη","Τετάρτη","Πέμπτη","Παρασκευή","Σάββατο","Κυριακή"]
    _DAY_EN = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    try:
        weekday   = _dt.strptime(target_date, "%Y-%m-%d").weekday()  # 0=Mon
        day_names = {_nrm(_DAY_GR[weekday]), _nrm(_DAY_EN[weekday])}
        for k, v in business.get("hours", {}).items():
            if _nrm(k) in day_names:
                vn = _nrm(v)
                if vn.startswith("κλειστ") or vn == "closed":
                    return business["day_start"], business["day_end"], True
                # Support both "10:00 - 20:00" and "10:00-20:00"
                import re as _re
                parts = _re.split(r'\s*-\s*', v.strip())
                if len(parts) == 2:
                    return parts[0].strip(), parts[1].strip(), False
    except Exception:
        pass
    return business["day_start"], business["day_end"], False


def handle_tool(slug: str, business: dict, name: str, inputs: dict):
    # ── get_slots ──────────────────────────────────────────────────────────────
    if name == "get_slots":
        try:
            service_name = str(inputs.get("service") or "")
            target_date  = str(inputs.get("date") or "")
            if not service_name or not target_date:
                return "Χρειάζομαι υπηρεσία και ημερομηνία."
            day_start, day_end, is_closed = _get_day_hours(business, target_date)
            if is_closed:
                return f"Είμαστε κλειστά αυτή την ημέρα. Δοκίμασε άλλη ημερομηνία."
            service  = _find_service(business, service_name)
            duration = service["duration"] if service else 60
            slots    = get_available_slots(
                slug, target_date, duration,
                day_start, day_end, business["slot_minutes"],
                closed_days=business.get("closed_days", []),
                blocked_dates=business.get("blocked_dates", []),
            )
            if not slots:
                return f"Δεν υπάρχουν διαθέσιμες ώρες για {target_date}. Δοκίμασε άλλη ημερομηνία."
            return f"Διαθέσιμες ώρες για {target_date}: {', '.join(slots)}"
        except Exception as e:
            print(f"[TOOL ERROR] get_slots: {e}")
            return "Δεν μπόρεσα να ελέγξω τη διαθεσιμότητα."

    # ── book_appointment ───────────────────────────────────────────────────────
    if name == "book_appointment":
        try:
            sn     = str(inputs.get("service") or "")
            date_  = str(inputs.get("date") or "")
            time_  = str(inputs.get("time") or "")
            name_  = str(inputs.get("name") or "")
            phone_ = str(inputs.get("phone") or "")
            email_ = str(inputs.get("email") or "")
            notes_ = str(inputs.get("notes") or "")
            if not all([sn, date_, time_, name_, phone_]):
                return "Λείπουν στοιχεία. Χρειάζομαι: υπηρεσία, ημερομηνία, ώρα, όνομα, τηλέφωνο."
            service = _find_service(business, sn)
            if not service:
                avail = ", ".join(s["name"] for s in business["services"])
                return f"Η υπηρεσία '{sn}' δεν βρέθηκε. Διαθέσιμες: {avail}"
            try:
                booking_id = create_booking(
                    slug=slug, date=date_, time=time_, service=sn,
                    duration=service["duration"], name=name_, phone=phone_,
                    email=email_, notes=notes_
                )
            except ValueError as ve:
                if "double_booking" in str(ve):
                    return f"Η ώρα {time_} στις {date_} έχει ήδη κλειστεί. Επέλεξε άλλη ώρα."
                raise
            booking = {
                "id": booking_id, "date": date_, "time": time_, "service": sn,
                "duration": service["duration"], "name": name_, "phone": phone_,
                "email": email_, "notes": notes_
            }
            def _async():
                try:
                    send_confirmation(booking, business)
                    # calendar sync κεντράρεται πλέον μέσα στο create_booking (single source) —
                    # ΟΛΑ τα paths (chat/voice/agentic) το παίρνουν, κανένα διπλό event.
                except Exception as e:
                    print(f"[ASYNC EMAIL ERROR] {e}")
            threading.Thread(target=_async, daemon=True).start()
            # ── ΣΥΝΔΕΣΗ ΜΕ BACK OFFICE: φάκελος ασθενή + graph link, ΑΜΕΣΑ (όπως το voice) ──
            # Χωρίς αυτό, η chat κράτηση φαινόταν στο «Ημέρα» (bookings) αλλά ΟΧΙ στα
            # «Φάκελοι» (bo_patients) μέχρι το nightly sync_patients_from_bookings.
            try:
                import back_office as _bo
                _bo.upsert_patient(slug, phone_, name_, email_)
                _bo.link(slug, "patient", phone_, "booking", booking_id, "appointment")
            except Exception as bo_err:
                print(f"[chat book_appointment] back-office patient link skipped: {bo_err}")
            try:
                update_client_after_booking(slug, phone_, name_, email_, sn, date_)
            except Exception as e:
                print(f"[CLIENT UPDATE ERROR] {e}")
            return f"Ραντεβού #{booking_id} κλείστηκε επιτυχώς!"
        except Exception as e:
            print(f"[TOOL ERROR] book_appointment: {e}")
            return "Δεν μπόρεσα να κλείσω το ραντεβού. Δοκίμασε ξανά."

    # ── save_client_notes ──────────────────────────────────────────────────────
    if name == "save_client_notes":
        try:
            upsert_client_profile(
                slug=slug, phone=str(inputs.get("phone") or ""),
                preferences=inputs.get("preferences"),
                skin_notes=inputs.get("skin_notes")
            )
            return "Οι σημειώσεις αποθηκεύτηκαν."
        except Exception as e:
            return "Οι σημειώσεις αποθηκεύτηκαν."

    # ── find_my_bookings ───────────────────────────────────────────────────────
    if name == "find_my_bookings":
        try:
            phone    = str(inputs.get("phone") or "")
            bookings = find_bookings_by_phone(slug, phone)
            if not bookings:
                return "Δεν βρέθηκαν επερχόμενα ραντεβού για αυτό το τηλέφωνο."
            lines = [f"Έχεις {len(bookings)} επερχόμενο ραντεβού:"]
            for b in bookings:
                lines.append(f"  • booking_id={b['id']} | {b['service']} — {b['date']} στις {b['time']}")
            return "\n".join(lines)
        except Exception as e:
            return "Δεν μπόρεσα να βρω τα ραντεβού."

    # ── reschedule_booking ─────────────────────────────────────────────────────
    if name == "reschedule_booking":
        try:
            booking_id = inputs.get("booking_id")
            phone      = str(inputs.get("phone") or "")
            new_date   = str(inputs.get("new_date") or "")
            new_time   = str(inputs.get("new_time") or "")
            if not all([booking_id, phone, new_date, new_time]):
                return "Λείπουν στοιχεία για αλλαγή ραντεβού."
            success = reschedule_booking(int(booking_id), phone, new_date, new_time)
            if success:
                return f"Το ραντεβού μεταφέρθηκε στις {new_date} {new_time}."
            return "Δεν βρέθηκε το ραντεβού."
        except ValueError:
            return "Αυτή η ώρα είναι ήδη πιασμένη — διάλεξε άλλη."
        except Exception as e:
            print(f"[RESCHEDULE ERROR] {e}", flush=True)
            return "Δεν μπόρεσα να αλλάξω το ραντεβού."

    # ── lookup_client ──────────────────────────────────────────────────────────
    if name == "lookup_client":
        try:
            phone    = str(inputs.get("phone") or "")
            profile  = get_client_profile(slug, phone)
            upcoming = find_bookings_by_phone(slug, phone)
            if not profile:
                return {"found": False}
            history      = profile.get("history") or []
            last_service = history[0]["service"] if history else None
            return {
                "found": True, "name": profile.get("name", ""),
                "last_service": last_service,
                "visit_count": profile.get("visit_count", 0),
                "last_visit": profile.get("last_visit", ""),
                "preferences": profile.get("preferences", ""),
                "history": history[:3],
                "upcoming_bookings": upcoming
            }
        except Exception as e:
            return {"found": False}

    # ── cancel_appointment ─────────────────────────────────────────────────────
    if name == "cancel_appointment":
        try:
            phone      = str(inputs.get("phone") or "")
            booking_id = inputs.get("booking_id")
            if booking_id:
                booking = get_booking(int(booking_id))
                if not booking or str(booking.get("phone")) != phone or booking.get("status") == "cancelled":
                    return {"success": False, "error": "Δεν βρέθηκε ενεργό ραντεβού."}
            else:
                upcoming = find_bookings_by_phone(slug, phone)
                if not upcoming:
                    return {"success": False, "error": "Δεν βρέθηκε ενεργό ραντεβού."}
                if len(upcoming) > 1:
                    lines = ["Βρέθηκαν πολλά ραντεβού. Ποιο να ακυρωθεί;"]
                    for b in upcoming:
                        lines.append(f"  • #{b['id']}: {b['service']} — {b['date']} στις {b['time']}")
                    return {"success": False, "needs_clarification": True, "message": "\n".join(lines)}
                booking = get_booking(upcoming[0]["id"])
            if not booking:
                return {"success": False, "error": "Δεν βρέθηκε το ραντεβού."}
            cancel_booking(booking["id"])
            booking_copy = dict(booking)
            threading.Thread(target=lambda: notify_waitlist(slug, booking_copy), daemon=True).start()
            return {"success": True, "service": booking_copy.get("service"),
                    "date": booking_copy.get("date"), "time": booking_copy.get("time")}
        except Exception as e:
            print(f"[TOOL ERROR] cancel_appointment: {e}")
            return {"success": False, "error": "Πρόβλημα κατά την ακύρωση."}

    # ── add_to_waitlist ────────────────────────────────────────────────────────
    if name == "add_to_waitlist":
        try:
            date_    = str(inputs.get("date") or "")
            time_    = str(inputs.get("time") or "")
            service_ = str(inputs.get("service") or "")
            name_    = str(inputs.get("name") or "")
            phone_   = str(inputs.get("phone") or "")
            email_   = str(inputs.get("email") or "")
            if not all([date_, time_, service_, name_, phone_]):
                return {"success": False, "error": "Λείπουν στοιχεία."}
            wid = db_add_to_waitlist(slug, date_, time_, service_, name_, phone_, email_)
            return {"success": True, "waitlist_id": wid}
        except Exception as e:
            return {"success": False, "error": "Δεν μπόρεσα να σε προσθέσω στη λίστα."}

    return f"Άγνωστο εργαλείο: {name}"


def _extract_phone(history: list):
    import re
    pattern = re.compile(r'\b(6\d{9}|\+30\s*6\d{9}|0030\s*6\d{9})\b')
    for msg in reversed(history):
        content = msg.get("content", "")
        if isinstance(content, str):
            m = pattern.search(content)
            if m:
                return re.sub(r'\D', '', m.group())[-10:]
    return None


def _hash_ip(ip: str) -> str:
    """Privacy-preserving hash για audit logs (no plain IP stored)."""
    if not ip:
        return ""
    salt = os.environ.get("IP_HASH_SALT", "ran-tevu-default-salt")
    return hashlib.sha256((salt + str(ip)).encode()).hexdigest()[:16]


# ════════════════════════════════════════════════════════════════════
#  CHAT ENDPOINT  (per business)
# ════════════════════════════════════════════════════════════════════

# Deterministic medical-emergency safety net (independent of the LLM).
_HEALTH_KEYS = (
    'ιατρ', 'γιατρ', 'κλινικ', 'clinic', 'medical', 'doctor', 'φυσιοθερ', 'φυσικοθερ',
    'physio', 'οδοντ', 'dental', 'κτηνιατ', 'vet', 'ψυχολ', 'ψυχιατρ', 'διαιτ', 'παιδίατ',
    'δερματ', 'καρδιολ', 'παθολ', 'γυναικολ', 'ορθοπ', 'νευρολ', 'ουρολ', 'οφθαλμ',
    'λογοθερ', 'health', 'υγεί',
)
_EMERGENCY_PATTERNS = (
    'πόνος στο στήθος', 'πόνο στο στήθος', 'σφίξιμο στο στήθος', 'δύσπνοια', 'δυσκολία να αναπν',
    'δεν μπορώ να αναπν', 'δεν αναπνέ', 'λιποθυμ', 'λιγοθυμ', 'αιμορραγ', 'εγκεφαλικ', 'έμφραγμα',
    'αναφυλαξ', 'αλλεργικό σοκ', 'αυτοκτον', 'να βλάψω τον εαυτό', 'δεν ξυπνά', 'χάνει τις αισθήσεις',
)

import unicodedata as _ud

def _strip_accents(s: str) -> str:
    """Remove Greek diacritics so 'πονο' matches 'πόνο' (users type without tonos)."""
    return "".join(c for c in _ud.normalize("NFD", s or "") if _ud.category(c) != "Mn")

def _is_health_business(business) -> bool:
    bt = _strip_accents((business.get('type') or '').lower())
    return any(_strip_accents(k) in bt for k in _HEALTH_KEYS)

def _looks_emergency(text: str) -> bool:
    t = _strip_accents((text or '').lower())
    return any(_strip_accents(p) in t for p in _EMERGENCY_PATTERNS)

def run_chat_turn(slug, business, messages, lang, memory=None, memory_identifier=None, client_profile=None):
    """Ο tool-calling loop του chat brain — Claude + εργαλεία (book/cancel/reschedule/...),
    μέχρι να έχει τελικό κείμενο απάντησης. Επιστρέφει ΚΕΙΜΕΝΟ (str), όχι Flask response,
    ώστε να το μοιράζονται ΚΑΙ το web chat (/api/<slug>/chat) ΚΑΙ το Unified Inbox
    (WhatsApp/Instagram) — ίδιος εγκεκριμένος εγκέφαλος, όχι δύο παράλληλες υλοποιήσεις."""
    max_iterations = 10
    iteration      = 0
    while iteration < max_iterations:
        iteration += 1
        # Prompt caching: το system prompt + tools κάνουν cache για 5 λεπτά,
        # ώστε επόμενα μηνύματα στην ίδια συνομιλία να κοστίζουν 10x λιγότερο σε input.
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": build_system_prompt(business, client_profile, lang, memory=memory),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=_build_tools(slug),
            messages=messages
        )
        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            final_reply = text or "Συγγνώμη, δοκίμασε ξανά."
            # Async memory write — δεν blockάρει το response
            try:
                full_msgs = messages + [{"role": "assistant", "content": final_reply}]
                store_conversation_memory(slug, full_msgs, async_mode=True)
            except Exception as _mem_err:
                print(f"[memory] write failed: {_mem_err}", flush=True)
            # Audit log (EU AI Act requirement)
            try:
                log_audit(
                    action="chat_completed",
                    slug=slug,
                    model="claude-sonnet-4-5",
                    customer_id=memory_identifier,
                    ip_hash=_hash_ip(get_remote_address()),
                    details=f"msgs={len(messages)} lang={lang}"
                )
            except Exception:
                pass
            return final_reply

        if response.stop_reason == "tool_use":
            assistant_content = response.content
            tool_results = []
            for block in assistant_content:
                if block.type == "tool_use":
                    try:
                        result = handle_tool(slug, business, block.name, block.input or {})
                    except Exception as e:
                        result = "Προέκυψε πρόβλημα. Δοκίμασε ξανά."
                    if not isinstance(result, str):
                        result = json.dumps(result, ensure_ascii=False)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result
                    })
            if not tool_results:
                break
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user",      "content": tool_results})
            continue

        text = next((b.text for b in (response.content or []) if hasattr(b, "text")), "")
        if text:
            return text
        break

    return "Συγγνώμη, δεν μπόρεσα να επεξεργαστώ το αίτημά σου. Δοκίμασε ξανά ή επικοινώνησε τηλεφωνικά."


@app.post("/api/<slug>/chat")
@limiter.limit("30 per minute; 200 per hour; 500 per day")
def chat(slug):
    business = get_business(slug)
    if not business:
        return jsonify({"reply": "Η υπηρεσία δεν είναι διαθέσιμη."}), 404
    try:
        data = request.get_json(silent=True, force=True)
        if not data:
            return jsonify({"reply": "Δεν έλαβα μήνυμα."}), 400
        history = data.get("history", [])
        # Lang detection: business field > request body > slug suffix > default "el"
        req_lang = data.get("lang") if data.get("lang") in ("el", "en") else None
        biz_lang = business.get("lang") if business.get("lang") in ("el", "en") else None
        slug_lang = "en" if slug.endswith("-en") else None
        lang = biz_lang or req_lang or slug_lang or "el"
        if not isinstance(history, list):
            return jsonify({"reply": "Μη έγκυρη μορφή."}), 400
        if len(history) > 40:
            history = history[-40:]
        messages = []
        for m in history:
            role    = m.get("role", "")
            content = m.get("content", "")
            if not isinstance(role, str) or not isinstance(content, str):
                continue
            if role not in ("user", "assistant"):
                continue
            messages.append({"role": role, "content": content[:2000]})

        # ── Deterministic medical-emergency net (runs BEFORE the LLM) ──────────
        if _is_health_business(business):
            _last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
            if _looks_emergency(_last_user):
                return jsonify({"reply": (
                    "🚨 Αυτό ακούγεται επείγον. Καλέστε ΑΜΕΣΩΣ το **166** (ΕΚΑΒ) ή το **112**, "
                    "ή πηγαίνετε στα επείγοντα του πλησιέστερου νοσοκομείου. "
                    "Δεν χειρίζομαι επείγοντα ιατρικά περιστατικά — η ασφάλειά σας προέχει."
                )})

        phone          = _extract_phone(history)
        client_profile = get_client_profile(slug, phone) if phone else None

        # ── AI Memory: fetch existing memory για αυτόν τον customer ──────────
        memory, memory_identifier = fetch_memory_for_prompt(slug, messages)

        reply = run_chat_turn(slug, business, messages, lang,
                              memory=memory, memory_identifier=memory_identifier,
                              client_profile=client_profile)
        return jsonify({"reply": reply})

    except anthropic.APIConnectionError:
        return jsonify({"reply": "Πρόβλημα σύνδεσης. Δοκίμασε ξανά σε λίγο."}), 503
    except anthropic.RateLimitError:
        return jsonify({"reply": "Πολλά αιτήματα. Δοκίμασε σε λίγα δευτερόλεπτα."}), 429
    except anthropic.APIStatusError as e:
        import traceback; traceback.print_exc()
        print(f"[ANTHROPIC ERROR] status={e.status_code} body={e.body}", flush=True)
        return jsonify({"reply": "Προσωρινό πρόβλημα. Δοκίμασε ξανά."}), 503
    except Exception as e:
        try:
            from debug_log import log_error
            log_error(f"chat({slug})", e)
        except Exception:
            import traceback; traceback.print_exc()
        return jsonify({"reply": "Κάτι πήγε στραβά. Δοκίμασε ξανά."})


# ════════════════════════════════════════════════════════════════════
#  ADMIN ENDPOINTS (per business)
# ════════════════════════════════════════════════════════════════════

@app.get("/api/<slug>/bookings")
@require_business_admin
def bookings(slug):
    return jsonify(get_all_bookings(slug))


@app.patch("/api/<slug>/settings")
@require_business_admin
def update_settings(slug):
    data    = request.json or {}
    allowed = ["name", "phone", "address", "owner_email", "owner_name",
               "smtp_email", "smtp_password", "tagline", "accent_color",
               "google_review_url", "questionnaire", "closed_days", "blocked_dates",
               "day_start", "day_end", "slot_minutes"]
    payload = {k: v for k, v in data.items() if k in allowed}
    if not payload:
        return jsonify({"error": "No updatable fields"}), 400
    update_business(slug, payload)
    return jsonify({"success": True})


@app.delete("/api/<slug>/bookings/<int:booking_id>")
@require_business_admin
def cancel_admin(slug, booking_id):
    booking = get_booking(booking_id)
    if not booking or booking.get("slug") != slug:
        return jsonify({"success": False, "error": "Δεν βρέθηκε"}), 404
    cancel_booking(booking_id)
    booking_copy = dict(booking)
    business     = get_business(slug)
    def _async():
        try:
            notify_waitlist(slug, booking_copy)
            if business and booking_copy.get("email"):
                send_cancellation_to_client(booking_copy, business)
        except Exception as e:
            print(f"[CANCEL ADMIN ASYNC ERROR] {e}")
    threading.Thread(target=_async, daemon=True).start()
    return jsonify({"success": True})


@app.get("/api/<slug>/info")
def business_info(slug):
    business = get_business(slug)
    if not business:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "name":             business["name"],
        "type":             business.get("type", ""),
        "tagline":          business.get("tagline", ""),
        "phone":            business["phone"],
        "address":          business.get("address", ""),
        "services":         [s["name"] for s in business["services"]],
        "service_prices":   {s["name"]: s["price"] for s in business["services"]},
        "hours":            business.get("hours", {}),
        "en_service_names": business.get("en_service_names", {}),
        # smtp_email ΑΦΑΙΡΕΘΗΚΕ από το public /info (PII leak) — admin settings prefill δεν το χρειάζεται
        "accent_color":       business.get("accent_color") or "#7C3AED",
        "google_review_url":  business.get("google_review_url", ""),
        "questionnaire":      business.get("questionnaire", ""),
        "closed_days":        business.get("closed_days", []),
        "blocked_dates":      business.get("blocked_dates", []),
        "lang":               business.get("lang") or ("en" if slug.endswith("-en") else "el"),
    })


@app.get("/api/<slug>/bookings/export")
@require_business_admin
def export_bookings_csv(slug):
    import csv, io
    from flask import Response
    bookings = get_all_bookings(slug, limit=10000)
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(["ID","Ημερομηνία","Ώρα","Υπηρεσία","Διάρκεια (λεπτά)","Πελάτης","Τηλέφωνο","Email","Κατάσταση","Δημιουργήθηκε"])
    for b in bookings:
        w.writerow([b["id"], b["date"], b["time"], b["service"], b["duration"],
                    b["name"], b["phone"], b.get("email",""), b["status"], b.get("created","")])
    output = out.getvalue()
    return Response(
        output.encode("utf-8-sig"),   # utf-8-sig = Excel-friendly BOM
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=randevou-{slug}.csv"}
    )


@app.get("/api/<slug>/confirm/<int:booking_id>")
def confirm_from_email(slug, booking_id):
    """1-click επιβεβαίωση από email link."""
    t = request.args.get("t", "")
    if t != _booking_token(booking_id, "confirm"):
        abort(403)
    booking  = get_booking(booking_id)
    business = get_business(slug)
    biz_name = business["name"] if business else slug
    if not booking or booking.get("slug") != slug:
        abort(404)
    status = booking.get("status")
    if status == "cancelled":
        icon, msg, color = "😔", "Αυτό το ραντεβού έχει ήδη ακυρωθεί.", "#EF4444"
    else:
        icon  = "✅"
        msg   = f"Ραντεβού επιβεβαιώθηκε! Σε περιμένουμε <strong>{booking['date']}</strong> στις <strong>{booking['time']}</strong>."
        color = "#10B981"
    return f"""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{biz_name}</title></head>
<body style="margin:0;padding:40px 20px;background:#f3f4f8;font-family:-apple-system,sans-serif;text-align:center;">
<div style="max-width:400px;margin:0 auto;background:#fff;border-radius:20px;padding:44px 32px;box-shadow:0 4px 32px rgba(0,0,0,.10);">
<div style="font-size:56px;margin-bottom:16px">{icon}</div>
<h2 style="font-size:20px;margin:0 0 14px;color:#111827">{biz_name}</h2>
<p style="font-size:15px;color:{color};font-weight:600;margin:0;line-height:1.6">{msg}</p>
</div></body></html>"""


@app.get("/api/<slug>/cancel-booking/<int:booking_id>")
def cancel_from_email(slug, booking_id):
    """1-click ακύρωση από email link."""
    t = request.args.get("t", "")
    if t != _booking_token(booking_id, "cancel"):
        abort(403)
    booking  = get_booking(booking_id)
    business = get_business(slug)
    biz_name = business["name"] if business else slug
    if not booking or booking.get("slug") != slug:
        abort(404)
    if booking.get("status") == "cancelled":
        msg = "Το ραντεβού είχε ήδη ακυρωθεί."
    else:
        cancel_booking(booking_id)
        booking_copy = dict(booking)
        threading.Thread(target=lambda: notify_waitlist(slug, booking_copy), daemon=True).start()
        msg = f"Το ραντεβού της <strong>{booking['date']}</strong> ({booking['service']}) ακυρώθηκε επιτυχώς."
    return f"""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{biz_name}</title></head>
<body style="margin:0;padding:40px 20px;background:#f3f4f8;font-family:-apple-system,sans-serif;text-align:center;">
<div style="max-width:400px;margin:0 auto;background:#fff;border-radius:20px;padding:44px 32px;box-shadow:0 4px 32px rgba(0,0,0,.10);">
<div style="font-size:56px;margin-bottom:16px">👋</div>
<h2 style="font-size:20px;margin:0 0 14px;color:#111827">{biz_name}</h2>
<p style="font-size:15px;color:#374151;margin:0 0 20px;line-height:1.6">{msg}</p>
<a href="/{slug}" style="display:inline-block;background:#7C3AED;color:#fff;text-decoration:none;
   padding:12px 24px;border-radius:20px;font-size:14px;font-weight:600;">📅 Κλείσε νέο ραντεβού</a>
</div></body></html>"""


@app.get("/claim/<slug>/<int:claim_id>")
def claim_waitlist_slot(slug, claim_id):
    """One-click claim από email waitlist offer."""
    from datetime import datetime
    t = request.args.get("t", "")
    if t != _claim_token(claim_id):
        abort(403)

    business = get_business(slug)
    if not business:
        abort(404)
    biz_name = business["name"]
    accent   = business.get("accent_color") or "#7C3AED"

    def _page(icon, title, msg, btn_text=None, btn_url=None):
        btn_html = ""
        if btn_text and btn_url:
            btn_html = (f'<a href="{btn_url}" style="display:inline-block;background:{accent};'
                        f'color:#fff;text-decoration:none;padding:12px 24px;border-radius:20px;'
                        f'font-size:14px;font-weight:600;margin-top:8px;">{btn_text}</a>')
        return (f"""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/><title>{biz_name}</title></head>
<body style="margin:0;padding:40px 20px;background:#f3f4f8;font-family:-apple-system,sans-serif;text-align:center;">
<div style="max-width:420px;margin:0 auto;background:#fff;border-radius:20px;padding:44px 32px;box-shadow:0 4px 32px rgba(0,0,0,.10);">
<div style="font-size:56px;margin-bottom:16px">{icon}</div>
<h2 style="font-size:22px;margin:0 0 12px;color:#111827">{title}</h2>
<p style="font-size:15px;color:#374151;margin:0 0 20px;line-height:1.6">{msg}</p>{btn_html}
</div></body></html>""")

    claim = get_pending_claim(claim_id)
    if not claim or claim.get("slug") != slug:
        return _page("🔍", "Δεν βρέθηκε", "Ο σύνδεσμος δεν είναι έγκυρος ή έχει ήδη χρησιμοποιηθεί.",
                     "Κλείσε ραντεβού", f"/{slug}"), 404

    # Έχει λήξει;
    expires = datetime.strptime(claim["expires_at"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() > expires:
        return _page("⏰", "Έληξε ο σύνδεσμος",
                     "Δυστυχώς, το χρονικό περιθώριο για να πάρεις τη θέση έληξε.",
                     "Κλείσε άλλο ραντεβού", f"/{slug}"), 410

    # Έχει ήδη χρησιμοποιηθεί; (atomic check)
    if not mark_claim_used(claim_id):
        return _page("✓", "Ήδη χρησιμοποιημένο",
                     "Αυτή η θέση έχει ήδη κλειστεί από εσένα ή έχει λήξει το link.",
                     "Κλείσε άλλο ραντεβού", f"/{slug}")

    # Βρες duration της υπηρεσίας
    duration = 30
    for s in business.get("services", []):
        if s.get("name") == claim["service"]:
            duration = int(s.get("duration", 30))
            break

    # Try create booking — αν είναι ήδη γεμάτο, race condition
    try:
        booking_id = create_booking(
            slug=slug, date=claim["date"], time=claim["time"],
            service=claim["service"], duration=duration,
            name=claim["name"], phone=claim["phone"],
            email=claim.get("email", ""), notes="from waitlist auto-claim"
        )
    except ValueError as e:
        if "double_booking" in str(e):
            return _page("😔", "Δυστυχώς πάρθηκε",
                         "Στο μεταξύ κάποιος άλλος έκλεισε αυτή τη θέση. Δοκίμασε άλλη ώρα.",
                         "Δες διαθέσιμες ώρες", f"/{slug}"), 409
        return _page("⚠️", "Σφάλμα", "Κάτι πήγε στραβά. Δοκίμασε ξανά.",
                     "Κλείσε ραντεβού", f"/{slug}"), 500

    # Στείλε confirmation email στον πελάτη
    try:
        booking = get_booking(booking_id)
        if booking:
            threading.Thread(target=lambda: send_confirmation(booking, business), daemon=True).start()
    except Exception as e:
        print(f"[CLAIM CONFIRM ERR] {e}")

    return _page("🎉", "Έκλεισες τη θέση!",
                 f"Έκλεισες <strong>{claim['service']}</strong> για "
                 f"<strong>{claim['date']}</strong> στις <strong>{claim['time']}</strong>. "
                 f"Θα λάβεις email επιβεβαίωσης σε λίγο.",
                 "Δες τις λεπτομέρειες", f"/{slug}")


@app.get("/q/<slug>/<int:booking_id>")
def questionnaire_page(slug, booking_id):
    """Φόρμα pre-visit questionnaire."""
    t = request.args.get("t", "")
    if t != _booking_token(booking_id, "questionnaire"):
        abort(403)
    booking  = get_booking(booking_id)
    if not booking or booking.get("slug") != slug:
        abort(404)
    business = get_business(slug)
    if not business:
        abort(404)
    question = business.get("questionnaire") or "Έχεις κάτι να μας πεις πριν το ραντεβού σου;"
    biz_name = business["name"]
    return f"""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{biz_name}</title></head>
<body style="margin:0;padding:32px 20px;background:#f3f4f8;font-family:-apple-system,sans-serif;">
<div style="max-width:480px;margin:0 auto;background:#fff;border-radius:20px;padding:36px 28px;box-shadow:0 4px 24px rgba(0,0,0,.08);">
  <div style="font-size:36px;margin-bottom:12px;text-align:center">📋</div>
  <h2 style="font-size:18px;text-align:center;margin:0 0 6px;color:#111827">{biz_name}</h2>
  <p style="font-size:13px;color:#9ca3af;text-align:center;margin:0 0 24px">
    {booking['date']} · {booking['time']} · {booking['service']}
  </p>
  <form method="POST" action="/q/{slug}/{booking_id}?t={t}">
    <label style="font-size:14px;font-weight:600;color:#374151;display:block;margin-bottom:8px">
      💬 {question}
    </label>
    <textarea name="answer" rows="4" maxlength="500"
      placeholder="π.χ. αλλεργία στη χρωστική, προτιμώ κοντά μαλλιά, ερώτηση για τιμές..."
      style="width:100%;border:1.5px solid #E5E7EB;border-radius:10px;padding:12px;
             font-size:14px;outline:none;resize:vertical;box-sizing:border-box;font-family:inherit"></textarea>
    <button type="submit"
      style="width:100%;margin-top:16px;background:linear-gradient(135deg,#e91e8c,#c2185b);
             color:#fff;border:none;border-radius:28px;padding:14px;font-size:15px;
             font-weight:700;cursor:pointer">
      ✅ Αποστολή
    </button>
  </form>
</div></body></html>"""


@app.post("/q/<slug>/<int:booking_id>")
def questionnaire_submit(slug, booking_id):
    """Αποθηκεύει απάντηση questionnaire."""
    t = request.args.get("t", "")
    if t != _booking_token(booking_id, "questionnaire"):
        abort(403)
    booking = get_booking(booking_id)
    if not booking or booking.get("slug") != slug:
        abort(404)
    business = get_business(slug)
    biz_name = business["name"] if business else slug
    answer = (request.form.get("answer") or "").strip()[:500]
    if answer:
        upsert_client_profile(slug, booking["phone"], preferences=answer)
    return f"""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{biz_name}</title></head>
<body style="margin:0;padding:40px 20px;background:#f3f4f8;font-family:-apple-system,sans-serif;text-align:center;">
<div style="max-width:400px;margin:0 auto;background:#fff;border-radius:20px;padding:44px 32px;box-shadow:0 4px 32px rgba(0,0,0,.10);">
<div style="font-size:56px;margin-bottom:16px">🙏</div>
<h2 style="font-size:20px;margin:0 0 14px;color:#111827">{biz_name}</h2>
<p style="font-size:15px;color:#374151;margin:0;line-height:1.6">
  Ευχαριστούμε πολύ! Θα είμαστε έτοιμοι για σένα στις <strong>{booking['date']} {booking['time']}</strong>. 💕
</p>
</div></body></html>"""


@app.post("/api/<slug>/campaigns/reactivate")
@require_business_admin
def manual_reactivate(slug):
    """Manual trigger reactivation campaign για συγκεκριμένη επιχείρηση."""
    business = get_business(slug)
    if not business:
        return jsonify({"error": "Not found"}), 404
    base    = _base_url()
    clients = get_clients_for_reengagement(slug, days=45, limit=100)
    count   = 0
    for client in clients:
        full = get_client_profile(slug, client["phone"])
        if full:
            client["history"] = full.get("history", [])
        send_reengagement_email(client, f"{base}/{slug}", business)
        mark_outreach_sent(slug, client["phone"])
        count += 1
    return jsonify({"success": True, "sent": count})


@app.get("/api/<slug>/campaigns/stats")
@require_business_admin
def campaign_stats(slug):
    """Πόσοι πελάτες θα στεχθούν αν τρέξει reactivation."""
    clients = get_clients_for_reengagement(slug, days=45, limit=1000)
    return jsonify({"reactivation_eligible": len(clients)})


@app.get("/api/ical/<int:booking_id>")
def download_ical(booking_id):
    from flask import Response
    # HMAC-gated: booking_id is a sequential integer, so without this an attacker could
    # enumerate 1..N and harvest patient name + clinic phone/address for every tenant.
    # Same signed-link pattern as the confirm/cancel routes (constant-time compare).
    t = request.args.get("t", "")
    if not hmac.compare_digest(t, _booking_token(booking_id, "ical")):
        abort(403)
    b = get_booking(booking_id)
    if not b:
        return "Not found", 404
    business = get_business(b["slug"])
    if not business:
        return "Not found", 404
    start = datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M")
    end   = start + timedelta(minutes=b.get("duration", 60))
    now   = datetime.utcnow()
    fmt   = "%Y%m%dT%H%M%S"
    ics = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//FrontdeskAI//GR",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "BEGIN:VEVENT",
        f"UID:booking-{b['id']}@frontdeskai",
        f"DTSTAMP:{now.strftime(fmt)}Z",
        f"DTSTART:{start.strftime(fmt)}", f"DTEND:{end.strftime(fmt)}",
        f"SUMMARY:{b['service']} — {business['name']}",
        f"DESCRIPTION:Ραντεβού\\n{b['name']}\\nΤηλ: {business['phone']}",
        f"LOCATION:{business['address']}",
        "STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR",
    ])
    return Response(ics, mimetype="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=rantevou-{b['id']}.ics"})


# ════════════════════════════════════════════════════════════════════
#  MASTER ADMIN — διαχείριση επιχειρήσεων
# ════════════════════════════════════════════════════════════════════

@app.get("/api/admin/businesses")
@require_master
def list_all_businesses():
    return jsonify(list_businesses())


@app.post("/api/admin/businesses")
@require_master
def add_business():
    data = request.json or {}
    slug = data.get("slug", "").strip().lower().replace(" ", "-")
    if not slug:
        return jsonify({"error": "slug required"}), 400
    if get_business(slug):
        return jsonify({"error": "slug already exists"}), 409
    if not data.get("admin_token"):
        data["admin_token"] = secrets.token_hex(16)
    bid = create_business(slug, data)
    # Send setup confirmation email to owner (non-blocking)
    owner_email = data.get("owner_email", "")
    if owner_email:
        def _send_setup():
            try:
                send_setup_confirmation(
                    owner_email=owner_email,
                    owner_name=data.get("owner_name", ""),
                    business_name=data.get("name", slug),
                    slug=slug,
                    admin_token=data["admin_token"],
                    base_url=_base_url(),
                )
            except Exception as e:
                print(f"[SETUP EMAIL ERROR] {e}")
        threading.Thread(target=_send_setup, daemon=True).start()
    return jsonify({"success": True, "id": bid, "slug": slug,
                    "admin_token": data["admin_token"],
                    "chat_url": f"/{slug}",
                    "admin_url": f"/admin/{slug}"}), 201


@app.get("/api/admin/businesses/<slug>")
@require_master
def get_business_detail(slug):
    biz = get_business(slug)
    if not biz:
        return jsonify({"error": "Not found"}), 404
    return jsonify(biz)


@app.put("/api/admin/businesses/<slug>")
@require_master
def edit_business(slug):
    data = request.json or {}
    update_business(slug, data)
    return jsonify({"success": True})


@app.patch("/api/admin/businesses/<slug>/rename")
@require_master
def rename_business(slug):
    data     = request.json or {}
    new_slug = data.get("new_slug", "").strip().lower().replace(" ", "-")
    if not new_slug:
        return jsonify({"error": "new_slug required"}), 400
    ok = rename_slug(slug, new_slug)
    if not ok:
        return jsonify({"error": "rename failed — old slug not found or new slug already exists"}), 409
    return jsonify({"success": True, "slug": new_slug,
                    "chat_url": f"/{new_slug}", "admin_url": f"/admin/{new_slug}"})


@app.patch("/api/admin/businesses/<slug>/toggle")
@require_master
def toggle_business(slug):
    biz = get_business(slug)
    if not biz:
        return jsonify({"error": "Not found"}), 404
    update_business(slug, {"active": 0 if biz["active"] else 1})
    return jsonify({"success": True})


# ════════════════════════════════════════════════════════════════════
#  COLD EMAIL CAMPAIGN (runs on server, bypasses local CF block)
# ════════════════════════════════════════════════════════════════════

@app.get("/api/admin/campaign")
def run_campaign():
    """
    Trigger cold-email batch from the Railway server (Ireland IP, not blocked by Cloudflare).
    Usage: GET /api/admin/campaign?token=<ADMIN_TOKEN>&offset=0&limit=50&list=leads.csv

    GDPR-compliant: filters unsubscribes + sent log (dedup), one-click unsubscribe,
    List-Unsubscribe headers (RFC 8058), reply-to monitorable inbox.
    """
    import csv as _csv, urllib.request as _ureq, urllib.error as _uerr

    if not _admin_ok(request.args.get("token", "")):
        return jsonify({"error": "unauthorized"}), 403

    resend_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_key:
        return jsonify({"error": "RESEND_API_KEY not set"}), 500

    offset    = int(request.args.get("offset", 0))
    limit     = int(request.args.get("limit",  50))
    list_name = request.args.get("list", "leads.csv")
    if "/" in list_name or "\\" in list_name or ".." in list_name:
        return jsonify({"error": "invalid list name"}), 400

    # Read leads
    leads_path = os.path.join(BASE_DIR, "scraper", list_name)
    leads = []
    try:
        with open(leads_path, encoding="utf-8-sig", errors="ignore") as _f:
            for row in _csv.DictReader(_f):
                email = (row.get("email") or "").strip().lower()
                name  = (row.get("name")  or "").strip()
                if email and "@" in email:
                    leads.append({"email": email, "name": name})
    except FileNotFoundError:
        return jsonify({"error": f"{list_name} not found"}), 500

    # Φιλτράρισμα: αφαίρεσε unsubscribed + ήδη απεσταλμένα
    eligible = [l for l in leads if not is_unsubscribed(l["email"]) and not was_sent(l["email"])]
    batch    = eligible[offset: offset + limit]

    demo_url     = "https://www.ran-tevu.gr/universal-demo"
    video_url    = "https://www.loom.com/share/a42066026aac476f80663667c84338cf"
    privacy_url  = "https://www.ran-tevu.gr/privacy"
    reply_to     = "owner@example.com"
    sender_name  = "Κωνσταντίνος"

    results = {"ok": [], "fail": [], "skipped_unsub": 0, "skipped_sent": 0}

    # μετράμε πόσα παραλείπονται από όλα τα leads (όχι μόνο το batch)
    for l in leads:
        if is_unsubscribed(l["email"]):
            results["skipped_unsub"] += 1
        elif was_sent(l["email"]):
            results["skipped_sent"] += 1

    for lead in batch:
        email_addr = lead["email"]
        unsub_token = _unsub_token(email_addr)
        unsub_url   = f"https://www.ran-tevu.gr/unsubscribe?email={email_addr}&t={unsub_token}"
        unsub_get   = f"https://www.ran-tevu.gr/api/unsubscribe?email={email_addr}&t={unsub_token}"

        subject = "AI γραμματέας 24/7 για ραντεβού — δωρεάν δοκιμή"
        html = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;padding:32px 16px">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.06)">
<tr><td style="background:#7C3AED;padding:28px 32px">
  <p style="margin:0;color:#fff;font-size:22px;font-weight:800;letter-spacing:-.3px">Ran-Tevu</p>
  <p style="margin:4px 0 0;color:#DDD6FE;font-size:13px">AI γραμματέας για ραντεβού</p>
</td></tr>
<tr><td style="padding:32px">
  <p style="margin:0 0 16px;font-size:15px;color:#111">Καλησπέρα,</p>
  <p style="margin:0 0 16px;font-size:15px;color:#374151;line-height:1.6">
    Είμαι ο {sender_name} από το <strong>Ran-Tevu</strong>. Φτιάξαμε ένα AI που κλείνει ραντεβού αυτόματα για επιχειρήσεις όπως η δική σας — 24 ώρες το 24ωρο, χωρίς να σηκώνετε τηλέφωνο.
  </p>
  <p style="margin:0 0 16px;font-size:15px;color:#374151;line-height:1.6">
    Ο πελάτης γράφει "θέλω ραντεβού", το AI του δείχνει τις διαθέσιμες ώρες, κλείνει, στέλνει confirmation και υπενθύμιση αυτόματα. Εσείς βλέπετε όλα τα ραντεβού σε ένα dashboard.
  </p>
  <table cellpadding="0" cellspacing="0" style="margin:24px 0">
    <tr><td style="background:#7C3AED;border-radius:10px">
      <a href="{demo_url}" style="display:block;padding:14px 28px;color:#fff;font-size:15px;font-weight:700;text-decoration:none">Δείτε live demo →</a>
    </td></tr>
  </table>
  <table cellpadding="0" cellspacing="0" style="margin:0 0 16px">
    <tr><td style="background:#fff;border:2px solid #7C3AED;border-radius:10px">
      <a href="{video_url}" style="display:block;padding:12px 26px;color:#7C3AED;font-size:14px;font-weight:700;text-decoration:none">▶︎ Δείτε το βίντεο (90 sec)</a>
    </td></tr>
  </table>
  <p style="margin:0 0 8px;font-size:14px;color:#374151">Setup σε 5 λεπτά. Χωρίς δέσμευση, ακυρώνετε όποτε θέλετε.</p>
  <p style="margin:16px 0 0;font-size:14px;color:#374151">
    Αν σας ενδιαφέρει, απαντήστε σε αυτό το email ή καλέστε με στο <strong>698 7747434</strong> και θα σας στείλω αναλυτική προσφορά.
  </p>
</td></tr>
<tr><td style="background:#F9FAFB;padding:20px 32px;border-top:1px solid #E5E7EB">
  <p style="margin:0 0 8px;font-size:12px;color:#9CA3AF;line-height:1.6">
    Ran-Tevu · Κωνσταντίνος Γκιουλέκας · Ελλάδα · 📞 6900000000<br>
    Λάβατε αυτό το email επειδή η επιχείρησή σας έχει δημόσια διεύθυνση επικοινωνίας. Νομική βάση: έννομο συμφέρον B2B (άρ. 6§1στ GDPR).
  </p>
  <p style="margin:8px 0 0;font-size:12px;color:#9CA3AF">
    <a href="{unsub_url}" style="color:#7C3AED">Διαγραφή με ένα κλικ</a> ·
    <a href="{privacy_url}" style="color:#7C3AED">Πολιτική Απορρήτου</a>
  </p>
</td></tr>
</table></td></tr></table>
</body></html>"""

        # Plain-text version (καλύτερο deliverability + accessibility)
        text = (
            f"Καλησπέρα,\n\n"
            f"Είμαι ο {sender_name} από το Ran-Tevu. Φτιάξαμε ένα AI που κλείνει "
            f"ραντεβού αυτόματα 24/7 για επιχειρήσεις σαν τη δική σας.\n\n"
            f"Δείτε live demo: {demo_url}\n"
            f"Δείτε το βίντεο (90 sec): {video_url}\n\n"
            f"Setup σε 5 λεπτά, χωρίς δέσμευση. Αν σας ενδιαφέρει, απαντήστε εδώ "
            f"ή καλέστε με στο 6900000000.\n\n"
            f"— {sender_name}\n"
            f"📞 6900000000\n\n"
            f"---\n"
            f"Διαγραφή: {unsub_url}\n"
            f"Πολιτική απορρήτου: {privacy_url}"
        )

        payload = json.dumps({
            "from":     f"{sender_name} από Ran-Tevu <noreply@ran-tevu.gr>",
            "to":       [email_addr],
            "reply_to": reply_to,
            "subject":  subject,
            "html":     html,
            "text":     text,
            "headers": {
                "List-Unsubscribe":      f"<{unsub_get}>, <mailto:{reply_to}?subject=unsubscribe>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                "Precedence":            "bulk",
            },
        }).encode("utf-8")

        req = _ureq.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type":  "application/json",
                "User-Agent":    "Mozilla/5.0 (Ran-Tevu Mailer; +https://www.ran-tevu.gr)",
                "Accept":        "application/json",
            },
            method="POST",
        )
        try:
            with _ureq.urlopen(req, timeout=15) as resp:
                if resp.status in (200, 201):
                    results["ok"].append(email_addr)
                    mark_sent(email_addr, campaign="cold", status="ok")
                else:
                    results["fail"].append(email_addr)
                    mark_sent(email_addr, campaign="cold", status=f"http_{resp.status}")
        except _uerr.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            results["fail"].append(f"{email_addr} ({e.code}: {body[:80]})")
            mark_sent(email_addr, campaign="cold", status=f"http_{e.code}")
        except Exception as e:
            results["fail"].append(f"{email_addr} (err: {str(e)[:60]})")
            mark_sent(email_addr, campaign="cold", status="err")

        import time as _t; _t.sleep(0.6)

    return jsonify({
        "list":            list_name,
        "total_leads":     len(leads),
        "eligible":        len(eligible),
        "batch":           f"{offset}–{offset+len(batch)}",
        "sent_now":        len(results["ok"]),
        "failed":          len(results["fail"]),
        "skipped_unsub":   results["skipped_unsub"],
        "skipped_already": results["skipped_sent"],
        "total_sent_alltime": count_sent("cold"),
        "ok":              results["ok"],
        "fail":            results["fail"],
    })


@app.get("/api/admin/campaign/stats")
def admin_campaign_stats():
    """Στατιστικά της cold campaign + λίστα unsubscribes."""
    if not _admin_ok(request.args.get("token", "")):
        return jsonify({"error": "unauthorized"}), 403
    return jsonify({
        "total_sent":       count_sent("cold"),
        "unsubscribes":     list_unsubscribes(),
        "unsubscribe_count": len(list_unsubscribes()),
    })


# ════════════════════════════════════════════════════════════════════
#  STATIC FILES
# ════════════════════════════════════════════════════════════════════

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC   = os.path.join(BASE_DIR, "public")


# ── Unsubscribe (GDPR / one-click) ───────────────────────────────────
def _unsub_token(email: str) -> str:
    return hmac.new(_signing_secret(), f"unsub:{email.strip().lower()}".encode(), hashlib.sha256).hexdigest()[:24]


@app.post("/api/unsubscribe")
def api_unsubscribe_post():
    """One-click unsubscribe από το unsubscribe.html ή POST request από Gmail (RFC 8058)."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or request.form.get("email") or "").strip().lower()
    token = (data.get("token") or request.form.get("token") or "").strip()
    if not email or "@" not in email:
        return jsonify({"error": "Μη έγκυρο email."}), 400
    if token and token != _unsub_token(email):
        return jsonify({"error": "Μη έγκυρος σύνδεσμος."}), 403
    add_unsubscribe(email, source="link")
    return jsonify({"ok": True})


@app.get("/api/unsubscribe")
def api_unsubscribe_get():
    """Direct unsubscribe link (για mail clients που κάνουν GET στον List-Unsubscribe URL)."""
    email = (request.args.get("email") or "").strip().lower()
    token = (request.args.get("t") or "").strip()
    if not email or "@" not in email:
        abort(400)
    if token and token != _unsub_token(email):
        abort(403)
    add_unsubscribe(email, source="link")
    # επιστρέφω HTML σελίδα επιβεβαίωσης
    return f"""<!DOCTYPE html><html lang="el"><head><meta charset="UTF-8">
<title>Διαγραφή — Ran-Tevu</title>
<style>body{{font-family:-apple-system,sans-serif;text-align:center;padding:80px 20px;color:#111;background:#FAFAFA}}
.card{{max-width:420px;margin:0 auto;background:#fff;padding:40px;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,.08)}}
h1{{font-size:22px;margin:16px 0}}p{{color:#374151;line-height:1.6}}
a{{color:#7C3AED;text-decoration:none}}.ok{{width:64px;height:64px;background:#D1FAE5;color:#10B981;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:32px;margin:0 auto}}
</style></head><body><div class="card"><div class="ok">✓</div>
<h1>Διαγράφηκες με επιτυχία</h1>
<p>Η διεύθυνση <strong>{email}</strong> δεν θα λαμβάνει άλλα emails από εμάς.</p>
<p style="margin-top:20px"><a href="/">← Επιστροφή στην αρχική</a></p>
</div></body></html>""", 200, {"Content-Type": "text/html; charset=utf-8"}


@app.after_request
def _security_headers(resp):
    """Υψηλή ασφάλεια — headers σε ΚΑΘΕ απάντηση (HSTS, anti-clickjacking, no-sniff).
    setdefault: δεν πατάει headers που ορίζει κάποιο route μόνο του."""
    try:
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    except Exception:
        pass  # ποτέ να μη ρίξει την απάντηση για ένα header
    return resp


@app.errorhandler(429)
def rate_limit_handler(e):
    return jsonify({"reply": "Πολλά μηνύματα. Περίμενε λίγο."}), 429

@app.errorhandler(413)
def too_large(e):
    return jsonify({"reply": "Το μήνυμα είναι πολύ μεγάλο."}), 413

@app.errorhandler(500)
@app.errorhandler(Exception)
def _handle_uncaught(e):
    """Global catch: καθαρό log (Railway) με marker [ERROR-500] για γρήγορο διάγνωση +
    clean response (ΟΧΙ stack-trace leak). HTTPExceptions (404/401/429...) περνούν ως έχουν."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    import traceback
    print(f"[ERROR-500] {request.method} {request.path}\n{traceback.format_exc()}", flush=True)
    return jsonify({"error": "internal_error", "hint": "δες Railway logs: [ERROR-500]"}), 500


# (σημ.) το /health ορίζεται μία φορά ψηλά (γραμμή ~89)· το διπλό εδώ αφαιρέθηκε
# γιατί έριχνε τον server στο boot (AssertionError: overwriting endpoint 'health').


# ════════════════════════════════════════════════════════════════════
#  EU AI Act + GDPR COMPLIANCE ENDPOINTS
#  Wave: Privacy-preserving compute + EU AI Act compliance (mandatory Aug 2026)
# ════════════════════════════════════════════════════════════════════

@app.post("/api/compliance/consent")
@limiter.limit("20 per minute")
def compliance_consent():
    """Records user consent (AI disclosure, GDPR data processing)."""
    data = request.get_json(silent=True, force=True) or {}
    consent_type = (data.get("consent_type") or "ai_disclosure")[:50]
    identifier = (data.get("identifier") or "")[:200]
    slug = (data.get("slug") or "")[:80]
    accepted = bool(data.get("accepted", True))
    version = (data.get("version") or "1.0")[:20]
    log_consent(
        consent_type=consent_type,
        slug=slug or None,
        identifier=identifier or None,
        identifier_type=data.get("identifier_type", "session"),
        consent_version=version,
        accepted=accepted,
        ip_hash=_hash_ip(get_remote_address()),
        user_agent=request.headers.get("User-Agent", "")[:300],
    )
    log_audit(
        action="consent_recorded",
        slug=slug or None,
        customer_id=identifier or None,
        ip_hash=_hash_ip(get_remote_address()),
        details=f"type={consent_type} accepted={accepted}",
    )
    return jsonify({"ok": True, "recorded": True, "version": version})


@app.get("/api/compliance/export/<path:identifier>")
@limiter.limit("5 per minute")
def compliance_export(identifier):
    """
    GDPR Article 15 — Right of Access.
    Returns ALL data stored for a customer (phone or email).
    Auth: requires ?slug=<slug>&token=<MASTER_TOKEN> OR a per-customer claim token.
    """
    slug = request.args.get("slug", "").strip()
    token = request.args.get("token", "")
    if not slug or not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    data = export_customer_data(slug, identifier)
    log_audit(
        action="data_export",
        slug=slug,
        customer_id=identifier,
        ip_hash=_hash_ip(get_remote_address()),
        actor_type="admin",
        details=f"requested via API",
    )
    return jsonify(data)


@app.post("/api/compliance/delete/<path:identifier>")
@limiter.limit("5 per minute")
def compliance_delete(identifier):
    """
    GDPR Article 17 — Right to Erasure.
    Deletes ALL customer data: memory, profile, bookings.
    Auth: requires ?slug=<slug>&token=<MASTER_TOKEN>.
    """
    slug = request.args.get("slug", "").strip()
    token = request.args.get("token", "")
    if not slug or not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    deleted = delete_all_customer_data(slug, identifier)
    log_audit(
        action="data_deletion",
        slug=slug,
        customer_id=identifier,
        ip_hash=_hash_ip(get_remote_address()),
        actor_type="admin",
        details=f"deleted={deleted}",
    )
    return jsonify({"ok": True, "deleted": deleted})


@app.get("/api/compliance/audit")
@limiter.limit("10 per minute")
def compliance_audit():
    """
    EU AI Act audit log access.
    Auth: ?token=<MASTER_TOKEN>. Filters: slug, customer_id, limit.
    """
    token = request.args.get("token", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    logs = list_audit_logs(
        slug=request.args.get("slug") or None,
        customer_id=request.args.get("customer_id") or None,
        limit=min(int(request.args.get("limit", "100")), 1000),
    )
    return jsonify({"count": len(logs), "logs": logs})


@app.get("/compliance")
def compliance_page():
    """Public-facing compliance page (EU AI Act + GDPR disclosures)."""
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), "public"),
        "compliance.html",
    )


@app.get("/legal/<page>")
def legal_pages(page):
    """Terms / Privacy / DPA / Refunds pages."""
    allowed = {"terms", "privacy", "dpa", "refunds"}
    if page not in allowed:
        return ("Not Found", 404)
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), "public", "legal"),
        f"{page}.html",
    )


@app.get("/odontiatreio")
def odontiatreio_landing():
    """Niche landing for dentists (Hormozi: riches in niches)."""
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), "public"),
        "odontiatreio.html",
    )


@app.get("/xenodoxeio")
def xenodoxeio_landing():
    """Niche landing for boutique hotels (Crete-first sales focus)."""
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), "public"),
        "xenodoxeio.html",
    )


# ── Outbound Proactive AI admin endpoints (Feature #3) ────────────────

@app.get("/api/admin/<slug>/outbound/jobs")
def admin_outbound_jobs(slug):
    """Lists outbound jobs for a business. Admin-only."""
    if not MASTER_TOKEN or not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {MASTER_TOKEN}"):
        return jsonify({"error": "unauthorized"}), 401
    import database as _db
    status = request.args.get("status")
    jobs = _db.list_outbound_jobs(slug=slug, status=status, limit=100)
    return jsonify({"jobs": jobs, "count": len(jobs)})


@app.get("/api/admin/<slug>/value-report")
def admin_value_report(slug):
    """Proof-of-value metrics για ένα business (JSON ή HTML preview). Admin-only.
    ?days=7  ?format=html|json|sms"""
    _hdr_ok = MASTER_TOKEN and hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {MASTER_TOKEN}")
    _qry_ok = MASTER_TOKEN and hmac.compare_digest(request.args.get("token", ""), MASTER_TOKEN)
    if not (_hdr_ok or _qry_ok):
        return jsonify({"error": "unauthorized"}), 401
    from value_report import compute_value, format_value_html, format_value_sms
    days = int(request.args.get("days", 7))
    r = compute_value(slug, days)
    fmt = request.args.get("format", "json")
    if fmt == "html":
        return format_value_html(r)
    if fmt == "sms":
        return format_value_sms(r), 200, {"Content-Type": "text/plain; charset=utf-8"}
    return jsonify(r)


@app.post("/api/admin/<slug>/outbound/winback")
def admin_trigger_winback(slug):
    """Manual trigger: enqueue win-back calls for customers inactive ≥N days."""
    if not MASTER_TOKEN or not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {MASTER_TOKEN}"):
        return jsonify({"error": "unauthorized"}), 401
    from voice.outbound import enqueue_winback_for_inactive_customers
    payload = request.get_json(silent=True) or {}
    days = int(payload.get("days_inactive", 90))
    limit = int(payload.get("limit", 10))
    count = enqueue_winback_for_inactive_customers(slug, days_inactive=days, limit=limit)
    return jsonify({"enqueued": count, "slug": slug, "days_inactive": days})


@app.post("/api/admin/<slug>/outbound/process-now")
def admin_outbound_process_now(slug):
    """Force-run the outbound processor (for testing)."""
    if not MASTER_TOKEN or not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {MASTER_TOKEN}"):
        return jsonify({"error": "unauthorized"}), 401
    from voice.outbound import process_due_jobs
    stats = process_due_jobs(max_jobs=20)
    return jsonify(stats)


@app.get("/iatreio")
def iatreio_landing():
    # Retired offer page (per-booking pricing) — contradicts the live €179/€299 flat model.
    return redirect("/", code=302)


@app.get("/dental-us")
def dental_us_landing():
    # Retired US experiment ($299 + 30-day money-back we don't offer) — never show stale terms.
    return redirect("/", code=302)


@app.get("/premium")
def premium_landing():
    # Retired UI prototype with pre-2026 pricing (€99/€199) — the cinematic index superseded it.
    return redirect("/", code=302)


@app.get("/premium2")
def premium2_landing():
    # Retired UI prototype with pre-2026 pricing — the cinematic index superseded it.
    return redirect("/", code=302)


@app.get("/classic")
def classic_landing():
    """Old Greek landing (pre-cinematic rebuild). Kept for revert/compare."""
    resp = send_from_directory(
        os.path.join(os.path.dirname(__file__), "public"),
        "index_classic.html",
    )
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    return resp


@app.get("/api/admin/env-check")
def admin_env_check():
    """Verify which money-critical env vars are SET on Railway (shows length+prefix, never values)."""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401

    money_keys = [
        # Critical infra (must have)
        "RESEND_API_KEY", "ANTHROPIC_API_KEY", "ADMIN_TOKEN",
        # Recently added (verify these specifically)
        "CLARITY_PROJECT_ID", "HUNTER_API_KEY", "OPENAI_API_KEY", "APIFY_API_KEY",
        # Optional (nice to have)
        "GMAIL_USER", "GMAIL_APP_PASSWORD", "AGENTS_V2_ENABLED",
    ]
    status = {}
    for k in money_keys:
        v = os.environ.get(k, "")
        if v:
            # Show first 6 chars + length only (NEVER full value)
            status[k] = {"set": True, "preview": v[:6] + "...", "len": len(v)}
        else:
            status[k] = {"set": False}

    return jsonify({
        "ts": datetime.now().isoformat(),
        "env_check": status,
        "summary": {
            "total_checked": len(money_keys),
            "set_count": sum(1 for s in status.values() if s["set"]),
            "missing_count": sum(1 for s in status.values() if not s["set"]),
        }
    })


@app.get("/api/admin/db-check")
def admin_db_check():
    """Verify ότι το SQLite είναι σε persistent Railway volume + δεν χάνονται data.
    Auth: ?token=ADMIN_TOKEN"""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401

    from database import DB as _DB
    db_path = str(_DB)
    persistent = any(db_path.startswith(p) for p in ("/data", "/mnt", "/var/lib")) \
        or bool(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"))
    exists = os.path.exists(db_path)
    size_kb = round(os.path.getsize(db_path) / 1024, 1) if exists else 0
    writable = os.access(os.path.dirname(db_path) or ".", os.W_OK)

    counts = {}
    try:
        from database import _conn
        with _conn() as c:
            for t in ("businesses", "bookings", "subscriptions", "outbound_jobs"):
                try:
                    counts[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except Exception:
                    counts[t] = "n/a"
    except Exception as e:
        counts = {"error": str(e)}

    return jsonify({
        "db_path": db_path,
        "DB_PATH_env": os.environ.get("DB_PATH", "(not set)"),
        "RAILWAY_VOLUME_MOUNT_PATH": os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "(not set)"),
        "persistent": persistent,
        "verdict": "✅ PERSISTENT — data survives deploys" if persistent
                   else "🔴 EPHEMERAL — data wiped on every deploy! Add volume + DB_PATH",
        "file_exists": exists, "size_kb": size_kb, "dir_writable": writable,
        "row_counts": counts,
    })


@app.get("/api/admin/setup-demo-voice")
def admin_setup_demo_voice():
    """Στήνει το demo voice end-to-end: provision demo ιατρείο + Vapi assistant ΜΕ tools
    (check_availability/book) + δένει στο number. Idempotent. Auth: ?token=ADMIN_TOKEN."""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        import provisioning
        from voice import assistant_builder, vapi_client as vc
        number = request.args.get("number", "+12164748588")
        demo_slug = "rantevu-demo"
        # 1) provision ΚΑΘΑΡΟ demo physio (demo=False → 0 fake νούμερα) + reset τυχόν παλιών seed
        res = provisioning.provision(name="Φυσικοθεραπευτήριο Ρεθύμνου",
                                     vertical_key="physio", demo=False, slug=demo_slug)
        provisioning.reset_business_data(demo_slug)  # καθάρισε ό,τι fake υπήρχε από παλιό seed
        biz = get_business(demo_slug)
        if not biz:
            return jsonify({"ok": False, "error": "provision failed"}), 500
        # 2) assistant config ΜΕ tools + serverUrl=/webhook/<slug> + (current-date στο prompt)
        cfg = assistant_builder.build_assistant_config(biz, voice_key="madlen", assistant_name="Ράνια")  # γυναικεία φωνή Sarah + γυναικείο όνομα (ταιριάζουν)
        name = cfg["name"]
        existing = next((a for a in vc.list_assistants(limit=100) if a.get("name") == name), None)
        if existing:
            aid = existing["id"]; vc.update_assistant(aid, cfg)
            mode = "updated"
        else:
            aid = vc.create_assistant(cfg).get("id"); mode = "created"
        # 3) attach στο number
        attached = False
        nums = vc._request("GET", "/phone-number?limit=20")
        for n in (nums if isinstance(nums, list) else nums.get("data", [])):
            if n.get("number") == number:
                vc.update_phone_assistant(n["id"], aid); attached = True; break
        # 4) ΚΑΜΙΑ fake seed — το demo δείχνει 0 (καθαρό). Τα πραγματικά δεδομένα μπαίνουν
        #    από τον πελάτη (Excel import / χρήση). Ο Ντίνος ΔΕΝ θέλει fake νούμερα πουθενά.
        seed_ok = False
        # 5) έτοιμα login links (όπως ο πληρωμένος γιατρός) — με το μόνιμο dash_token
        from routes.dashboard_routes import dash_token
        base = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") and f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}") \
            or os.environ.get("BASE_URL", "").rstrip("/") or "https://www.ran-tevu.gr"
        dt = dash_token(demo_slug)
        login = {
            "dashboard_esoda": f"{base}/{demo_slug}/dashboard?t={dt}",
            "back_office": f"{base}/{demo_slug}/back-office?t={dt}",
            "front_desk_chat": f"{base}/{demo_slug}",
            "voice_number": number,
        }
        return jsonify({
            "ok": True, "assistant": mode, "assistant_id": aid,
            "demo_slug": demo_slug, "attached_to": number if attached else None,
            "back_office_seeded": seed_ok,
            "ΜΠΕΣ_ΣΑΝ_ΓΙΑΤΡΟΣ": login,
            "links": res.get("links", {}),
            "next": f"Άνοιξε τα links στο 'ΜΠΕΣ_ΣΑΝ_ΓΙΑΤΡΟΣ' + κάλεσε {number} και ζήτα ραντεβού.",
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)[:400]}), 500


@app.get("/api/admin/provision-voice")
def admin_provision_voice():
    """Ενεργοποιεί φωνητικό AI (Vapi) για ΥΠΑΡΧΟΝ ιατρείο — το «κουμπί» αντί για CLI.
    Auth: ?token=ADMIN_TOKEN. Params: slug=<slug> (υποχρ.), phone=+30… (reuse υπάρχον νούμερο,
    προαιρ.), purchase=1 (αγόρασε ΝΕΟ Twilio νούμερο — ΞΟΔΕΥΕΙ λεφτά, προαιρ.), area_code (προαιρ.).
    ⚠️ Χρειάζεται πρώτα VAPI_API_KEY + ELEVENLABS_API_KEY (+ Twilio) στο Railway env."""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    slug = (request.args.get("slug") or "").strip()
    if not slug:
        return jsonify({"ok": False, "error": "λείπει το slug"}), 400
    biz = get_business(slug)
    if not biz:
        return jsonify({"ok": False, "error": f"δεν βρέθηκε ενεργό ιατρείο '{slug}'"}), 404
    try:
        from voice import vapi_client
        from voice.provisioner import provision_voice_agent, ProvisionError
        if not vapi_client.is_configured():
            return jsonify({
                "ok": False, "keys_missing": True,
                "error": "Το φωνητικό AI χρειάζεται keys που δεν έχουν μπει ακόμα.",
                "how": "Railway → βάλε VAPI_API_KEY + ELEVENLABS_API_KEY (+ Twilio για νούμερο), μετά ξαναπάτα εδώ.",
            }), 400
        try:
            from voice.elevenlabs_client import pick_voice_for_business
            voice_key = pick_voice_for_business(biz) or "madlen"
        except Exception:
            voice_key = "madlen"
        purchase = request.args.get("purchase") in ("1", "true", "yes")
        phone = (request.args.get("phone") or "").strip() or None
        res = provision_voice_agent(
            business_slug=slug, voice_key=voice_key,
            assistant_name=(request.args.get("name") or "Ράνια").strip(),
            twilio_phone=phone, purchase_new_number=purchase,
            area_code=request.args.get("area_code", "2831"),
        )
        num = res.get("twilio_phone_number")
        return jsonify({
            "ok": True, "slug": slug, "voice_number": num,
            "vapi_assistant_id": res.get("vapi_assistant_id"),
            "next": (f"Η φωνή είναι ενεργή για το {slug}. Δώσε στον γιατρό το νούμερο {num}."
                     if num else
                     f"Ο assistant στήθηκε για το {slug}, αλλά ΔΕΝ έχει νούμερο. "
                     f"Ξαναπάτα με purchase=1 (αγορά νέου) ή phone=+30… (υπάρχον)."),
        })
    except ProvisionError as e:
        return jsonify({"ok": False, "error": f"Provisioning: {e}"}), 400
    except Exception as e:
        # Vapi/Twilio API errors (π.χ. λείπει νούμερο) — καθαρό 400 με οδηγία, όχι σκέτο 500
        emsg = str(e)[:400]
        low = emsg.lower()
        hint = ""
        if "area" in low or "phone-number" in low or "numberdesired" in low or "phone number" in low:
            hint = " — Χρειάζεται νούμερο: τσέκαρε «Αγόρασε νέο ελληνικό νούμερο» ή δώσε phone=+30… (υπάρχον Twilio νούμερο)."
        return jsonify({"ok": False, "error": emsg + hint}), 400


@app.get("/api/admin/setup-sales-demo")
def admin_setup_sales_demo():
    """Στήνει το 'ιατρείο' κρατήσεων DEMO του Ντίνου (dogfood): ο ίδιος ο Ran-Tevu AI
    κλείνει τα 20λεπτα sales demos. slug='book-demo'. Idempotent. Auth: ?token=ADMIN_TOKEN."""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        slug = "book-demo"
        owner_email = os.environ.get("FOUNDER_EMAIL", "owner@example.com")
        biz = get_business(slug)
        cfg = {
            "name": "Ran-Tevu — Κλείσε το Demo σου",
            "type": "demo",
            "tagline": "20λεπτη παρουσίαση του AI για το ιατρείο σου",
            "owner_email": owner_email, "owner_name": "Κωνσταντίνος",
            "phone": "", "address": "Online (Zoom/Meet)",
            "slot_minutes": 20, "day_start": "10:00", "day_end": "21:00",
            "closed_days": [6],   # Κυριακή κλειστά
            "services": [{"name": "Demo κλήση Ran-Tevu", "price": 0, "duration": 20}],
            "hours": {d: ("Κλειστά" if d == "Κυριακή" else "10:00 - 21:00")
                      for d in ["Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή"]},
        }
        if biz:
            update_business(slug, cfg)
        else:
            import secrets as _s
            cfg["admin_token"] = _s.token_hex(8)
            create_business(slug, cfg)
        base = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") and f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}") \
            or os.environ.get("BASE_URL", "").rstrip("/") or "https://www.ran-tevu.gr"
        return jsonify({"ok": True, "slug": slug, "booking_chat": f"{base}/{slug}",
                        "owner_email": owner_email,
                        "next": "Το κουμπί «Κλείσε δωρεάν demo» στο landing οδηγεί εδώ — ο AI κλείνει το demo σου."})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)[:400]}), 500


@app.get("/api/admin/setup-customer")
def admin_setup_customer():
    """SALES-LED: στήνει ΚΑΘΑΡΟ ιατρείο για ΠΡΑΓΜΑΤΙΚΟ πελάτη (demo=False → ΜΗΔΕΝ fake data,
    μηδενισμένα νούμερα) + επιστρέφει τα login links + welcome email. Auth: ?token=ADMIN_TOKEN.
    Params: name=Όνομα Ιατρείου, vertical=physio|dental|dermatology|medical, email=γιατρού."""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        import provisioning
        name = (request.args.get("name") or "Το Ιατρείο μου").strip()
        vertical = (request.args.get("vertical") or "physio").strip()
        email = (request.args.get("email") or "").strip()
        # demo=False → ΚΑΘΑΡΟ ιατρείο: μόνο services από template, ΚΑΝΕΝΑ fake ραντεβού/τιμολόγιο
        res = provisioning.provision(name=name, vertical_key=vertical, email=email, demo=False)
        slug = res.get("slug")
        links = res.get("links", {})
        sent = False
        if email:
            try:
                from routes.billing_routes import _send_welcome_email
                _send_welcome_email(email, name, links)
                sent = True
            except Exception as _we:
                print(f"[setup-customer] welcome email skipped: {_we}")
        return jsonify({
            "ok": True, "slug": slug, "clean": True, "welcome_email_sent": sent,
            "ΣΤΗΣΙΜΟ_ΠΕΛΑΤΗ": {
                "1_setup_ωρες_υπηρεσιες": links.get("setup"),
                "2_back_office_γραμματεας": links.get("back_office"),
                "3_dashboard_εσοδα": links.get("dashboard"),
                "4_chat_ασθενη": links.get("chat"),
            },
            "next": "Μηδενισμένα νούμερα. Στείλε στον γιατρό το (1) setup link να βάλει ώρες+υπηρεσίες, "
                    "μετά μπαίνει στο (2) back-office (Γραμματέας). Τα νούμερα γεμίζουν μόνα τους από τα ραντεβού.",
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)[:400]}), 500


@app.get("/api/admin/resend-links")
def admin_resend_links():
    """Ξαναδίνει (και προαιρετικά ξαναστέλνει) τα 4 login links ΥΠΑΡΧΟΝΤΟΣ ιατρείου —
    για όταν ο γιατρός έχασε το welcome email ή απέτυχε το Stripe webhook. Auth: ?token=ADMIN_TOKEN.
    Params: slug=<slug> (υποχρεωτικό), email=<override> (προαιρ.), send=1 (να σταλεί email).
    ΧΩΡΙΣ send=1 απλώς ΕΠΙΣΤΡΕΦΕΙ τα links — δεν στέλνει τίποτα (ασφαλές default)."""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    slug = (request.args.get("slug") or "").strip()
    if not slug:
        return jsonify({"ok": False, "error": "λείπει το slug"}), 400
    biz = get_business(slug)
    if not biz:
        return jsonify({"ok": False, "error": f"δεν βρέθηκε ενεργό ιατρείο με slug '{slug}'"}), 404
    import provisioning
    links = provisioning._links(slug)
    name = biz.get("name") or "Το Ιατρείο μου"
    email = (request.args.get("email") or biz.get("email") or "").strip()
    sent = False
    if request.args.get("send") in ("1", "true", "yes") and email:
        try:
            from routes.billing_routes import _send_welcome_email
            _send_welcome_email(email, name, links)
            sent = True
        except Exception as _we:
            print(f"[resend-links] welcome email skipped: {_we}")
    return jsonify({
        "ok": True, "slug": slug, "email": email, "welcome_email_sent": sent,
        "links": {
            "1_setup_ωρες_υπηρεσιες": links.get("setup"),
            "2_back_office_γραμματεας": links.get("back_office"),
            "3_dashboard_εσοδα": links.get("dashboard"),
            "4_chat_ασθενη": links.get("chat"),
        },
        "next": ("Αντίγραψε τα links και στείλ' τα στον γιατρό. Πρόσθεσε &send=1 στο URL "
                 "για αυτόματο welcome email (χρειάζεται RESEND_API_KEY στο Railway)."),
    })


@app.post("/api/admin/email-raw")
def admin_email_raw():
    """Στέλνει arbitrary email στον founder από Railway (Greek IPs blocked για Resend locally).
    Χρησιμοποιείται από το news agent. Auth: ?token=ADMIN_TOKEN. Body: {subject, html}."""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    d = request.get_json(silent=True) or {}
    subject = (d.get("subject") or "Ran-Tevu").strip()
    html = d.get("html") or d.get("text") or ""
    if not html:
        return jsonify({"error": "empty"}), 400
    try:
        from email_service import send_email_via_resend
        to = os.environ.get("FOUNDER_EMAIL", "owner@example.com")
        ok = send_email_via_resend(to, subject, html)
        return jsonify({"sent": bool(ok), "to": to})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@app.get("/api/admin/email-techdoc")
def admin_email_techdoc():
    """Στέλνει το Technical Documentation (Word+md) στον Ντίνο. Auth: ?token=ADMIN_TOKEN"""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        import base64 as _b64
        atts = []
        for fn in ("Ran-Tevu_Technical_Documentation.docx", "Ran-Tevu_Technical_Documentation.md"):
            p = os.path.join(os.path.dirname(__file__), fn)
            if os.path.exists(p):
                with open(p, "rb") as f:
                    atts.append({"filename": fn, "content": _b64.b64encode(f.read()).decode()})
        if not atts:
            return jsonify({"error": "files not found"}), 404
        html = ("<div style='font-family:sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1c1917'>"
                "<h2 style='color:#064e3b'>Τεχνικη Τεκμηριωση Ran-Tevu</h2>"
                "<p>Ντινο, πως δουλευει ΚΑΘΕ αυτοματοποιηση απο πισω:</p>"
                "<ul style='line-height:1.8'><li>9 αυτοματοποιησεις: τι κερδιζει ο γιατρος + connector + συναρτησεις + data flow + επιστημονικη πηγη (PubMed RCTs)</li>"
                "<li>Architecture · Connector map (7 tools) · Back-office (δουλειες γραμματειας) · Enterprise positioning</li></ul>"
                "<p style='color:#78716c;font-size:13px'>Αυτο ειναι το προιον, οχι site. — Ran-Tevu</p></div>")
        resend_key = os.environ.get("RESEND_API_KEY", "")
        from_domain = os.environ.get("FROM_DOMAIN", "ran-tevu.gr")
        payload = json.dumps({
            "from": f"Ran-Tevu <noreply@{from_domain}>",
            "to": ["owner@example.com"], "reply_to": "owner@example.com",
            "subject": "Τεχνικη Τεκμηριωση - πως δουλευει καθε αυτοματοποιηση",
            "html": html, "attachments": atts,
        }).encode("utf-8")
        import urllib.request as _ur, urllib.error as _ue
        req = _ur.Request("https://api.resend.com/emails", data=payload, headers={
            "Authorization": f"Bearer {resend_key}", "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
            "Accept": "application/json"}, method="POST")
        try:
            with _ur.urlopen(req, timeout=20) as r:
                return jsonify({"sent": True, "to": "owner@example.com", "files": len(atts), "status": r.status})
        except _ue.HTTPError as e:
            return jsonify({"sent": False, "error": f"HTTP {e.code}", "detail": e.read().decode()[:300]}), 200
        except Exception as e:
            return jsonify({"sent": False, "error": str(e)}), 200
    except Exception as outer:
        return jsonify({"sent": False, "error": "crash", "detail": str(outer)[:300]}), 200


@app.get("/api/admin/email-decision")
def admin_email_decision():
    """Στέλνει το Decision Dashboard Excel (δείκτες 4 verticals + sales playbook). Auth: ?token=ADMIN_TOKEN"""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        import base64 as _b64
        p = os.path.join(os.path.dirname(__file__), "Ran-Tevu_Decision_Dashboard.xlsx")
        if not os.path.exists(p):
            return jsonify({"error": "file not found"}), 404
        with open(p, "rb") as f:
            b64 = _b64.b64encode(f.read()).decode()
        html = ("<div style='font-family:sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1c1917'>"
                "<h2 style='color:#064e3b'>Decision Dashboard — που παμε απο τα 4</h2>"
                "<p>Ντινο, το Excel για να δεις ΕΣΥ τους δεικτες:</p>"
                "<ul style='line-height:1.8'>"
                "<li><b>Sheet 1 Decision Dashboard:</b> 11 δεικτες ανα vertical. ΑΛΛΑΞΕ τα ΒΑΡΗ (κιτρινη στηλη) → το τελικο σκορ ξαναϋπολογιζεται μονο του + chart</li>"
                "<li><b>Sheet 2:</b> ποιος κερδιζει πιο πολυ απο το Ran-Tevu (Physio 10/10)</li>"
                "<li><b>Sheet 3 Sales Playbook:</b> το 4-Stage Closer PDF εφαρμοσμενο — Belief Diagnostic, DAC objections, 5 Systems</li></ul>"
                "<p style='color:#78716c;font-size:13px'>Δες τους δεικτες, αλλαξε βαρη, πες μου 'ξεκινα' για την αυτοματοποιηση. — Ran-Tevu</p></div>")
        resend_key = os.environ.get("RESEND_API_KEY", "")
        from_domain = os.environ.get("FROM_DOMAIN", "ran-tevu.gr")
        payload = json.dumps({
            "from": f"Ran-Tevu <noreply@{from_domain}>",
            "to": ["owner@example.com"], "reply_to": "owner@example.com",
            "subject": "Decision Dashboard - που παμε απο τα 4 verticals (Excel)",
            "html": html, "attachments": [{"filename": "Ran-Tevu_Decision_Dashboard.xlsx", "content": b64}],
        }).encode("utf-8")
        import urllib.request as _ur, urllib.error as _ue
        req = _ur.Request("https://api.resend.com/emails", data=payload, headers={
            "Authorization": f"Bearer {resend_key}", "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
            "Accept": "application/json"}, method="POST")
        try:
            with _ur.urlopen(req, timeout=20) as r:
                return jsonify({"sent": True, "to": "owner@example.com", "status": r.status})
        except _ue.HTTPError as e:
            return jsonify({"sent": False, "error": f"HTTP {e.code}", "detail": e.read().decode()[:300]}), 200
        except Exception as e:
            return jsonify({"sent": False, "error": str(e)}), 200
    except Exception as outer:
        return jsonify({"sent": False, "error": "crash", "detail": str(outer)[:300]}), 200


@app.get("/api/admin/email-diary")
def admin_email_diary():
    """Στέλνει το FULL Day-in-Life Word (4 verticals) στον Ντίνο. Auth: ?token=ADMIN_TOKEN"""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        import base64 as _b64
        atts = []
        for fn in ("Ran-Tevu_Verticals_FULL_Day_in_Life.docx", "Ran-Tevu_Verticals_FULL_Day_in_Life.md"):
            p = os.path.join(os.path.dirname(__file__), fn)
            if os.path.exists(p):
                with open(p, "rb") as f:
                    atts.append({"filename": fn, "content": _b64.b64encode(f.read()).decode()})
        if not atts:
            return jsonify({"error": "files not found"}), 404
        html = ("<div style='font-family:sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1c1917'>"
                "<h2 style='color:#064e3b'>Πως λειτουργει καθε επιχειρηση απο μεσα</h2>"
                "<p>Ντινο, η ΠΛΗΡΗΣ αναλυση (Word + markdown) — 4 verticals, ωρα-ωρα:</p>"
                "<ul style='line-height:1.8'><li>Physio · Γιατροι/Dental · Beauty · Γυμναστηρια</li>"
                "<li>Καθε ατομο/ρολος, καθε software, καθε ενεργεια στο laptop, καθε τμημα</li>"
                "<li>Full day-in-life (8πμ→βραδυ) + εβδομαδιαια + μηνιαια + που μπαινει το Ran-Tevu</li></ul>"
                "<p style='color:#78716c;font-size:13px'>Αυτο ειναι ΟΛΗ η αναλυση, οχι σκορ. — Ran-Tevu</p></div>")
        resend_key = os.environ.get("RESEND_API_KEY", "")
        from_domain = os.environ.get("FROM_DOMAIN", "ran-tevu.gr")
        payload = json.dumps({
            "from": f"Ran-Tevu <noreply@{from_domain}>",
            "to": ["owner@example.com"], "reply_to": "owner@example.com",
            "subject": "Πληρης αναλυση - πως λειτουργει καθε επιχειρηση (4 verticals)",
            "html": html, "attachments": atts,
        }).encode("utf-8")
        import urllib.request as _ur, urllib.error as _ue
        req = _ur.Request("https://api.resend.com/emails", data=payload, headers={
            "Authorization": f"Bearer {resend_key}", "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
            "Accept": "application/json"}, method="POST")
        try:
            with _ur.urlopen(req, timeout=20) as r:
                return jsonify({"sent": True, "to": "owner@example.com", "files": len(atts), "status": r.status})
        except _ue.HTTPError as e:
            return jsonify({"sent": False, "error": f"HTTP {e.code}", "detail": e.read().decode()[:300]}), 200
        except Exception as e:
            return jsonify({"sent": False, "error": str(e)}), 200
    except Exception as outer:
        return jsonify({"sent": False, "error": "crash", "detail": str(outer)[:300]}), 200


@app.get("/api/admin/email-ops")
def admin_email_ops():
    """Στέλνει το Verticals Operations Excel στον Ντίνο (Railway/Resend — local μπλοκάρει).
    Auth: ?token=ADMIN_TOKEN"""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        import base64 as _b64
        path = os.path.join(os.path.dirname(__file__), "Ran-Tevu_Verticals_Operations.xlsx")
        if not os.path.exists(path):
            return jsonify({"error": "xlsx not found in repo"}), 404
        with open(path, "rb") as f:
            b64 = _b64.b64encode(f.read()).decode()
        html = ("<div style='font-family:sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1c1917'>"
                "<h2 style='color:#064e3b'>Verticals Operations Breakdown</h2>"
                "<p>Ντινο, το Excel με τα 4 verticals (Physio, Γιατροι/Dental, Beauty, Gym-Pilates):</p>"
                "<ul style='line-height:1.8'><li>42 tasks, click-by-click flow</li>"
                "<li>Τι software, τι αυτοματοποιειται, opportunity score</li>"
                "<li>Sheet 'Συγκριση' = ranking (Physio 88 · Gym 71 · Dental 62 · Beauty 61)</li>"
                "<li>#1 revenue leak + time-waster ανα vertical</li></ul>"
                "<p style='color:#78716c;font-size:13px'>Το insider cheat-sheet για τις κλησεις. — Ran-Tevu</p></div>")
        resend_key = os.environ.get("RESEND_API_KEY", "")
        from_domain = os.environ.get("FROM_DOMAIN", "ran-tevu.gr")
        payload = json.dumps({
            "from": f"Ran-Tevu <noreply@{from_domain}>",
            "to": ["owner@example.com"], "reply_to": "owner@example.com",
            "subject": "Verticals Operations - 4 niches breakdown (Excel)",
            "html": html,
            "attachments": [{"filename": "Ran-Tevu_Verticals_Operations.xlsx", "content": b64}],
        }).encode("utf-8")
        import urllib.request as _ur, urllib.error as _ue
        req = _ur.Request("https://api.resend.com/emails", data=payload, headers={
            "Authorization": f"Bearer {resend_key}", "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
            "Accept": "application/json"}, method="POST")
        try:
            with _ur.urlopen(req, timeout=15) as r:
                return jsonify({"sent": True, "to": "owner@example.com", "status": r.status})
        except _ue.HTTPError as e:
            return jsonify({"sent": False, "error": f"HTTP {e.code}", "detail": e.read().decode()[:300]}), 200
        except Exception as e:
            return jsonify({"sent": False, "error": str(e)}), 200
    except Exception as outer:
        return jsonify({"sent": False, "error": "crash", "detail": str(outer)[:300]}), 200


@app.get("/api/admin/agents/<slug>")
def admin_agents(slug):
    """Agent observability: ποιοι AI agents τρέχουν στο ιατρείο + τι έκαναν (30d). Auth: ?token=ADMIN_TOKEN"""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        from clinic_agents import agents_summary
        from database import get_business as _gb
        if not _gb(slug):
            return jsonify({"error": "tenant not found"}), 404
        return jsonify(agents_summary(slug))
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 200


@app.get("/api/admin/seed-demo-data")
def admin_seed_demo_data():
    """Γεμίζει ένα demo tenant με 6μηνο ρεαλιστικά δεδομένα (bookings/no-shows/recovery/voice)
    ώστε το €-dashboard να δείχνει νούμερα σαν να τρέχει πραγματικά. ?slug=physio-demo&token=ADMIN_TOKEN"""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        from database import _conn, get_business
        slug = request.args.get("slug", "physio-demo")
        if not get_business(slug):
            return jsonify({"error": f"tenant {slug} not found — seed it first"}), 404
        svcs = [s.get("name") for s in (get_business(slug).get("services") or []) if isinstance(s, dict)] or ["Συνεδρία"]
        from datetime import datetime, timedelta
        now = datetime.now()
        bk = ns = rec = vc = 0
        with _conn() as c:
            already = c.execute("SELECT COUNT(*) FROM bookings WHERE slug=? AND notes LIKE '%[DEMO]%'", (slug,)).fetchone()[0]
            if already:
                return jsonify({"status": "already_seeded", "demo_bookings": already})
            for mo in range(5, -1, -1):           # 6 μήνες, παλιό→νέο
                base = now - timedelta(days=mo * 30)
                n_book = 20 + (mo % 3) * 2          # ~20-24 ραντεβού/μήνα (deterministic)
                for i in range(n_book):
                    d = (base - timedelta(days=i)).strftime("%Y-%m-%d")
                    status = "no_show" if i < 3 else "completed"
                    if status == "no_show":
                        ns += 1
                    c.execute("INSERT INTO bookings (slug,date,time,service,duration,name,phone,email,status,notes,reminder_24,reminder_4) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,1,1)",
                              (slug, d, "10:00", svcs[i % len(svcs)], 45, f"Ασθενής {mo}-{i}",
                               "690" + str(1000000 + mo * 100 + i), f"demo{mo}{i}@example.gr", status, "[VOICE][DEMO]"))
                    bk += 1
                for j in range(14):                 # voice calls answered
                    ts = (base - timedelta(days=j, hours=2)).strftime("%Y-%m-%d %H:%M:%S")
                    c.execute("INSERT INTO voice_calls (business_slug,from_number,started_at,duration_seconds,cost_usd,summary) "
                              "VALUES (?,?,?,?,?,?)", (slug, "690" + str(2000000 + mo * 100 + j), ts, 95, 0.12, "[DEMO] Έκλεισε ραντεβού"))
                    vc += 1
                n_rec = 6 if mo == 0 else 3          # recovered no-shows (περισσότερα φέτος για 30d headline)
                for k in range(n_rec):
                    ts = (base - timedelta(days=k)).strftime("%Y-%m-%d %H:%M:%S")
                    c.execute("INSERT INTO outbound_jobs (slug,job_type,channel,customer_phone,status,scheduled_at,created,result) "
                              "VALUES (?,?,?,?,?,?,?,?)",
                              (slug, "no_show_recovery", "sms", "690" + str(3000000 + mo * 100 + k), "done", ts, ts, "[DEMO] rebooked"))
                    rec += 1
            c.commit()
        return jsonify({"status": "seeded", "slug": slug, "bookings": bk, "no_shows": ns,
                        "recovered": rec, "voice_calls": vc, "dashboard": f"/{slug}/dashboard"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)[:400]}), 200


@app.get("/api/admin/seed-demo")
def admin_seed_demo():
    """Generic demo seeder για ΟΛΑ τα 4 verticals. ?vertical=physio|medical|beauty|fitness&token=ADMIN_TOKEN
    Engine horizontal — ίδιο codebase, vertical config από vertical_templates.py."""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        from vertical_templates import get_template, resolve_vertical
        from database import get_business as _gb, create_business as _cb
        vkey = resolve_vertical(request.args.get("vertical", "physio"))
        tmpl = get_template(vkey)
        slug = f"{vkey}-demo"
        if _gb(slug):
            return jsonify({"status": "exists", "vertical": vkey, "slug": slug, "chat": f"/{slug}"})
        cfg = {
            "name": f"{tmpl['label']} (Demo)", "type": tmpl["type"],
            "tagline": tmpl["label"], "phone": "2100000000", "address": "Αθήνα",
            "owner_email": "demo@ran-tevu.gr", "owner_name": "Demo",
            "hours": {d: "09:00 - 21:00" for d in ["Δευτέρα","Τρίτη","Τετάρτη","Πέμπτη","Παρασκευή"]},
            "services": tmpl["services"], "slot_minutes": 15,
            "day_start": "09:00", "day_end": "21:00", "admin_token": secrets.token_hex(8),
        }
        bid = _cb(slug, cfg)
        return jsonify({"status": "created", "vertical": vkey, "slug": slug, "id": bid, "chat": f"/{slug}",
                        "sales_focus": tmpl["sales_focus"]})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)[:300]}), 200


@app.get("/api/admin/seed-physio-demo")
def admin_seed_physio_demo():
    """Seed το physio-demo tenant ώστε το 'Try it live' chat (index.html SLUG) να μη σπάει.
    Idempotent. Auth: ?token=ADMIN_TOKEN"""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    from database import get_business as _gb, create_business as _cb
    SLUG = "physio-demo"
    if _gb(SLUG):
        return jsonify({"status": "exists", "slug": SLUG, "chat": f"/{SLUG}"})
    cfg = {
        "name": "Κέντρο Φυσικοθεραπείας (Demo)",
        "type": "φυσιοθεραπευτήριο",
        "tagline": "Αποκατάσταση & Φυσικοθεραπεία",
        "phone": "2100000000", "address": "Αθήνα",
        "owner_email": "demo@ran-tevu.gr", "owner_name": "Demo Physio",
        "hours": {"Δευτέρα": "09:00 - 21:00", "Τρίτη": "09:00 - 21:00", "Τετάρτη": "09:00 - 21:00",
                  "Πέμπτη": "09:00 - 21:00", "Παρασκευή": "09:00 - 21:00",
                  "Σάββατο": "09:00 - 14:00", "Κυριακή": "Κλειστά"},
        "services": [
            {"name": "Πρώτη Εκτίμηση & Αξιολόγηση", "duration": 30, "price": 30},
            {"name": "Συνεδρία Φυσικοθεραπείας", "duration": 45, "price": 40},
            {"name": "Αποκατάσταση Τραυματισμού", "duration": 60, "price": 50},
            {"name": "Manual Therapy", "duration": 45, "price": 45},
            {"name": "TECAR Therapy", "duration": 30, "price": 35},
            {"name": "Αθλητική Φυσικοθεραπεία", "duration": 60, "price": 50},
        ],
        "slot_minutes": 15, "day_start": "09:00", "day_end": "21:00",
        "admin_token": secrets.token_hex(8),
    }
    try:
        bid = _cb(SLUG, cfg)
        return jsonify({"status": "created", "slug": SLUG, "id": bid, "chat": f"/{SLUG}"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)[:300]}), 200


@app.get("/api/admin/email-leads")
def admin_email_leads():
    """Στέλνει τη λίστα physio leads (CSV, έτοιμη για cold calls) στον Ντίνο.
    Τρέχει από Railway (Resend δουλεύει — local Greek IP μπλοκάρεται). Auth: ?token=ADMIN_TOKEN"""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        import csv as _csv, base64 as _b64, io as _io
        csv_path = os.path.join(os.path.dirname(__file__), "scraper", "physio_leads.csv")
        if not os.path.exists(csv_path):
            return jsonify({"error": "leads csv not found"}), 404

        with open(csv_path, encoding="utf-8-sig") as _f:
            rows = [r for r in _csv.DictReader(_f) if r.get("phone", "").strip()]
        city_order = {"Αθήνα": 0, "Πειραιάς": 1, "Γλυφάδα": 1, "Καλλιθέα": 1, "Μαρούσι": 1,
                      "Χαλάνδρι": 1, "Θεσσαλονίκη": 2, "Πάτρα": 3}
        rows.sort(key=lambda r: (city_order.get(r.get("city", ""), 5), r.get("name", "")))

        def _btype(name):
            n = (name or "").lower()
            return "Κέντρο φυσικοθεραπείας" if any(w in n for w in
                  ["κέντρο", "center", "clinic", "κλινικ", "team", "αποκατάσταση"]) else "Φυσιοθεραπευτής (solo)"

        buf = _io.StringIO()
        buf.write("﻿")  # BOM → Excel ανοίγει ελληνικά σωστά
        w = _csv.writer(buf)
        w.writerow(["#", "Ονομα", "Τηλεφωνο", "Τυπος", "Πολη", "Email", "Website", "Status", "Σημειωσεις"])
        for i, r in enumerate(rows, 1):
            w.writerow([i, r.get("name", ""), r.get("phone", ""), _btype(r.get("name", "")),
                        r.get("city", ""), r.get("email", ""), r.get("website", ""), "", ""])
        b64 = _b64.b64encode(buf.getvalue().encode("utf-8")).decode()

        solo = sum(1 for r in rows if "solo" in _btype(r.get("name", "")))
        ath = sum(1 for r in rows if r.get("city") == "Αθήνα")
        html = (f"<div style='font-family:sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1c1917'>"
                f"<h2 style='color:#064e3b'>Λιστα κλησεων — {len(rows)} φυσιοθεραπευτες</h2>"
                f"<p>Ντινο, ετοιμη η λιστα για cold calling (CSV — ανοιγει σε Excel/Sheets):</p><ul style='line-height:1.8'>"
                f"<li><b>{len(rows)}</b> leads με τηλεφωνο</li><li><b>{len(rows)-solo}</b> κεντρα · <b>{solo}</b> solo</li>"
                f"<li><b>{ath}</b> Αθηνα (πρωτα — #1 market)</li>"
                f"<li>Στηλες <b>Status</b> + <b>Σημειωσεις</b> για καθε κληση</li></ul>"
                f"<p style='background:#f0fdf4;border-left:3px solid #16a34a;padding:12px 16px;border-radius:8px;font-size:14px'>"
                f"<b>Script:</b> «Καθε μερα χανεις 8-10 κλησεις οσο εισαι με ασθενη — οι μισες εκτος ωραριου. "
                f"Σου τις σηκωνω 24/7 και κλεινω το ραντεβου. 14 μερες δωρεαν, θες να το δεις;»</p>"
                f"<p style='color:#78716c;font-size:13px'>Στοχος: 5-10 κλησεις/μερα. — Ran-Tevu</p></div>")

        resend_key = os.environ.get("RESEND_API_KEY", "")
        from_domain = os.environ.get("FROM_DOMAIN", "ran-tevu.gr")
        payload = json.dumps({
            "from": f"Ran-Tevu <noreply@{from_domain}>",
            "to": ["owner@example.com"],
            "reply_to": "owner@example.com",
            "subject": f"{len(rows)} fysiotherapeutes gia klhseis - lista",
            "html": html,
            "attachments": [{"filename": "Ran-Tevu_Leads_CALLS.csv", "content": b64}],
        }).encode("utf-8")
        import urllib.request as _ur, urllib.error as _ue
        req = _ur.Request("https://api.resend.com/emails", data=payload,
                          headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json",
                                   "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
                                   "Accept": "application/json"}, method="POST")
        try:
            with _ur.urlopen(req, timeout=15) as r:
                return jsonify({"sent": True, "to": "owner@example.com", "leads": len(rows), "status": r.status})
        except _ue.HTTPError as e:
            return jsonify({"sent": False, "error": f"HTTP {e.code}", "detail": e.read().decode()[:300]}), 200
        except Exception as e:
            return jsonify({"sent": False, "error": str(e)}), 200
    except Exception as outer:
        return jsonify({"sent": False, "error": "endpoint_crash", "detail": str(outer)[:300]}), 200


@app.get("/api/admin/money_campaign")
def admin_money_campaign():
    """Trigger money_campaign.py from Railway server (bypasses Greek-IP Cloudflare blocks).
    Auth: ?token=ADMIN_TOKEN&cmd=<status|reset|score|send|reactivate|all>&limit=<int>"""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401

    cmd = request.args.get("cmd", "status")
    if cmd not in ("status", "reset", "score", "dryrun", "send", "reactivate", "all"):
        return jsonify({"error": f"invalid cmd: {cmd}"}), 400

    limit = int(request.args.get("limit", 10))

    import subprocess
    script_path = os.path.join(os.path.dirname(__file__), "scripts", "money_campaign.py")
    try:
        result = subprocess.run(
            ["python", script_path, cmd, str(limit)],
            capture_output=True, text=True, timeout=600,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        return jsonify({
            "ok": True, "cmd": cmd, "limit": limit,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-2000:] if result.stderr else "",
            "returncode": result.returncode,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "timeout after 600s"}), 504
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.get("/api/admin/config-check")
def admin_config_check():
    """Επιβεβαιώνει ότι η DB είναι στο persistent volume."""
    if not _admin_ok(request.args.get("token", "")):
        return jsonify({"error": "unauthorized"}), 403
    from database import DB
    db_path     = DB
    db_exists   = os.path.exists(db_path)
    db_size     = os.path.getsize(db_path) if db_exists else 0
    on_volume   = db_path.startswith("/data") or db_path.startswith("/var/data") or db_path.startswith("/mnt")
    env_db_path = os.environ.get("DB_PATH", "(not set)")
    volume_dir  = "/data"
    volume_writable = False
    try:
        os.makedirs(volume_dir, exist_ok=True)
        test_file = os.path.join(volume_dir, ".write_test")
        with open(test_file, "w") as _f:
            _f.write("ok")
        os.remove(test_file)
        volume_writable = True
    except Exception:
        volume_writable = False
    return jsonify({
        "db_path":         db_path,
        "db_exists":       db_exists,
        "db_size_kb":      db_size // 1024,
        "on_volume":       on_volume,
        "env_DB_PATH":     env_db_path,
        "volume_writable": volume_writable,
        "verdict": "✓ OK — DB στο volume" if on_volume and db_exists else (
            "✗ DB σε ephemeral filesystem — θα χαθεί στο επόμενο deploy"
        ),
    })


@app.get("/api/admin/daily-now")
def admin_daily_now():
    """Manual trigger του daily founder report. Test mode."""
    if not _admin_ok(request.args.get("token", "")):
        return jsonify({"error": "unauthorized"}), 403
    try:
        send_founder_daily_report()
        return jsonify({"ok": True, "message": "Daily report sent — check email"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/admin/agent-run")
def admin_agent_run():
    """Manual trigger για autonomous agents. Usage:
       GET /api/admin/agent-run?token=...&name=today|money|orchestrator|reality|replies"""
    if not _admin_ok(request.args.get("token", "")):
        return jsonify({"error": "unauthorized"}), 403
    name = (request.args.get("name") or "").strip().lower()
    try:
        from agents import AGENTS
        if name not in AGENTS:
            return jsonify({"error": f"unknown agent '{name}'", "available": list(AGENTS.keys())}), 400
        AGENTS[name]()
        return jsonify({"ok": True, "ran": name, "message": "check email"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/admin/backup-now")
def admin_backup_now():
    """Manual trigger backup (στείλε email αμέσως). Χρήσιμο για test."""
    if not _admin_ok(request.args.get("token", "")):
        return jsonify({"error": "unauthorized"}), 403
    try:
        weekly_db_backup()
        return jsonify({"ok": True, "message": "Backup started — check email."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _wants_english() -> bool:
    """Detects if browser prefers English over Greek via Accept-Language header.
    Returns True if EN comes before EL (or EL not in list)."""
    al = (request.headers.get("Accept-Language") or "").lower()
    if not al:
        return False
    if "el" not in al and "gr" not in al:
        return True
    # If both present, see which appears first
    el_pos = min((al.find(x) for x in ("el", "gr") if x in al), default=999)
    en_pos = al.find("en") if "en" in al else 999
    return en_pos >= 0 and en_pos < el_pos


@app.get("/")
def index():
    # Auto-redirect to /en for non-Greek browsers (unless explicitly forced via ?lang=el)
    if request.args.get("lang") != "el" and _wants_english():
        return redirect("/en", code=302)
    resp = send_from_directory(PUBLIC, "index.html")
    # Short public cache: crawlers/CDNs can cache briefly (SEO/perf) instead of re-fetching every
    # hit, while a deploy still shows up within 5 minutes. (Was no-store — hurt Core Web Vitals
    # and crawl efficiency for zero real benefit; redeploys are infrequent, not per-second.)
    resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    resp.headers["Vary"] = "Accept-Language"
    return resp


@app.get("/en")
def index_en():
    """English landing page (auto-served for non-Greek browsers, or via direct link)."""
    resp = send_from_directory(PUBLIC, "index_en.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/en/")
def index_en_slash():
    return redirect("/en", code=301)

@app.get("/demo-video.mp4")
def demo_video():
    return send_from_directory(PUBLIC, "demo-video.mp4", mimetype="video/mp4")


@app.get("/og-image.png")
def og_image_el():
    """Open Graph social card (Greek)."""
    resp = send_from_directory(PUBLIC, "og-image.png", mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/og-image-en.png")
def og_image_en():
    """Open Graph social card (English)."""
    resp = send_from_directory(PUBLIC, "og-image-en.png", mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# ════════════════════════════════════════════════════════════════════
#  INDEXNOW + Discovery (Bing/Yandex/Seznam instant indexing)
# ════════════════════════════════════════════════════════════════════

@app.get("/<path:filename>.txt")
def indexnow_key_file(filename):
    """Serves IndexNow key verification file (auto-detects which key)."""
    import re
    # Only allow hex string keys (security)
    if not re.match(r"^[a-f0-9]{8,128}$", filename):
        abort(404)
    key_path = os.path.join(PUBLIC, "indexnow-key.txt")
    if not os.path.exists(key_path):
        abort(404)
    with open(key_path, "r", encoding="utf-8") as f:
        actual_key = f.read().strip()
    if filename != actual_key:
        abort(404)
    return actual_key, 200, {"Content-Type": "text/plain"}


@app.get("/api/admin/email-batch/status")
def admin_email_batch_status():
    """Returns cold email batch readiness diagnostics — verifies tomorrow's run will work."""
    token = request.args.get("token", "") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    import csv as _csv
    diag = {}
    # 1. Resend key present
    diag["resend_key_set"] = bool(os.environ.get("RESEND_API_KEY"))
    # 2. Leads file exists + count
    leads_path = os.path.join(BASE_DIR, "scraper", "leads_master.csv")
    diag["leads_file_exists"] = os.path.exists(leads_path)
    if diag["leads_file_exists"]:
        with open(leads_path, encoding="utf-8-sig", errors="ignore") as f:
            leads = list(_csv.DictReader(f))
        diag["total_leads"] = len(leads)
        # Language split
        el_count = 0
        en_count = 0
        for r in leads:
            email = (r.get("email") or "").strip()
            name = (r.get("name") or "").strip()
            if not email:
                continue
            if _detect_lead_language(email, name) == "el":
                el_count += 1
            else:
                en_count += 1
        diag["leads_greek"] = el_count
        diag["leads_english"] = en_count
    # 3. Dedup count
    diag["already_sent"] = count_sent("cold")
    # 4. Eligible for tomorrow
    try:
        eligible = sum(
            1 for r in leads
            if (r.get("email") or "").strip()
            and not is_unsubscribed(r["email"])
            and not was_sent(r["email"])
        )
        diag["eligible_remaining"] = eligible
        diag["days_runway_at_80"] = round(eligible / 80, 1)
    except Exception:
        diag["eligible_remaining"] = None
    # 5. Scheduler job registered
    try:
        jobs = [j.id for j in _scheduler.get_jobs()]
        diag["scheduler_jobs"] = jobs
        diag["batch_job_registered"] = any("cold" in j or "email" in j.lower() for j in jobs)
    except Exception:
        diag["scheduler_jobs"] = []
    # 6. Next fire time
    try:
        for j in _scheduler.get_jobs():
            if "cold" in j.id.lower() or "email" in j.id.lower():
                diag[f"next_fire_{j.id}"] = str(j.next_run_time)
    except Exception:
        pass
    return jsonify(diag)


@app.post("/api/admin/email-batch/test")
def admin_email_batch_test():
    """
    Manual trigger του cold email batch.
    ?dry=1 → απλά returns what WOULD be sent χωρίς να στείλει.
    ?dry=0 → πραγματικά στέλνει (careful!).
    """
    token = request.args.get("token", "") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    dry = request.args.get("dry", "1") == "1"
    if dry:
        # Just simulate — return what would be sent (μετά από dedup)
        import csv as _csv, re as _re
        leads_path = os.path.join(BASE_DIR, "scraper", "leads_master.csv")
        with open(leads_path, encoding="utf-8-sig", errors="ignore") as f:
            leads_raw = list(_csv.DictReader(f))
        seen_dn = set()
        eligible = []
        for r in leads_raw:
            email = (r.get("email") or "").strip()
            name = (r.get("name") or "").strip()
            if not email or "@" not in email:
                continue
            if not _re.match(r"^[\w.+-]+@[\w-]+\.[\w.-]+$", email):
                continue
            domain = email.split("@", 1)[1].lower()
            key = (domain, name.lower())
            if key in seen_dn:
                continue
            seen_dn.add(key)
            if is_unsubscribed(email) or was_sent(email):
                continue
            eligible.append({
                "email": email,
                "name": name,
                "lang": _detect_lead_language(email, name),
            })
            if len(eligible) >= 10:
                break
        return jsonify({
            "dry_run": True,
            "would_send_to": eligible,
            "el_count": sum(1 for l in eligible if l["lang"] == "el"),
            "en_count": sum(1 for l in eligible if l["lang"] == "en"),
        })
    else:
        # Real send (limited to first 2 για safety)
        try:
            auto_cold_email_batch()
            return jsonify({"ok": True, "triggered": True, "check_logs": "Railway logs"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


@app.post("/api/admin/indexnow/push")
def admin_indexnow_push():
    """Admin endpoint: push core pages to IndexNow (Bing/Yandex pickup)."""
    token = request.args.get("token", "") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        from indexnow_service import push_core_pages, push_sitemap_urls
        scope = request.args.get("scope", "core")
        result = push_sitemap_urls() if scope == "all" else push_core_pages()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ════════════════════════════════════════════════════════════════════
#  TRUST FILES (Play Store TWA, humans.txt, security.txt)
# ════════════════════════════════════════════════════════════════════

@app.get("/.well-known/assetlinks.json")
def assetlinks_json():
    """Digital Asset Links — proves PWA owns the Android app για TWA (Play Store).
    Once you generate the Android app via PWABuilder, paste the SHA-256 fingerprint here."""
    return send_from_directory(
        os.path.join(PUBLIC, ".well-known"),
        "assetlinks.json",
        mimetype="application/json",
    )


@app.get("/.well-known/security.txt")
def security_txt():
    """RFC 9116 — vulnerability disclosure contact."""
    return send_from_directory(
        os.path.join(PUBLIC, ".well-known"),
        "security.txt",
        mimetype="text/plain",
    )


@app.get("/humans.txt")
def humans_txt():
    """humans.txt initiative — credits + team info."""
    return send_from_directory(PUBLIC, "humans.txt", mimetype="text/plain")


@app.get("/security.txt")
def security_txt_root():
    """Redirect to .well-known/security.txt (legacy clients)."""
    return redirect("/.well-known/security.txt", code=301)


@app.get("/browserconfig.xml")
def browserconfig_xml():
    """Microsoft tile config για pinned sites."""
    return send_from_directory(PUBLIC, "browserconfig.xml", mimetype="application/xml")


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    return send_from_directory(PUBLIC, "apple-touch-icon.png", mimetype="image/png")


@app.get("/favicon.png")
def favicon_png():
    return send_from_directory(PUBLIC, "favicon.png", mimetype="image/png")

# /demo is served by the back_office blueprint (routes/back_office_routes.py::public_demo) —
# a self-provisioning, write-blocked sandbox clinic. Kept out of app.py to avoid a duplicate
# route; the live chat-booking demo is still directly reachable at /mitropoleos.

@app.get("/setup")
def setup_page():
    return send_from_directory(PUBLIC, "setup.html")

@app.get("/pitch")
def pitch_page():
    """Investor / advisor pitch artifact (noindex, shared via DM)."""
    resp = send_from_directory(PUBLIC, "pitch.html")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp

@app.get("/vertical/<industry>")
def vertical_page(industry):
    """Industry-specific landing pages (SEO + targeted positioning)."""
    valid = {"dentist", "beauty", "vet", "physio", "restaurant"}
    if industry not in valid:
        abort(404)
    filename = f"vertical_{industry}.html"
    import os as _os_v
    if not _os_v.path.exists(_os_v.path.join(PUBLIC, filename)):
        filename = "vertical_dentist.html"
    resp = send_from_directory(PUBLIC, filename)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp

@app.get("/robots.txt")
def robots_txt():
    return send_from_directory(PUBLIC, "robots.txt", mimetype="text/plain")

@app.get("/llms.txt")
def llms_txt():
    return send_from_directory(PUBLIC, "llms.txt", mimetype="text/plain")

@app.get("/sitemap.xml")
def sitemap_xml():
    return send_from_directory(PUBLIC, "sitemap.xml", mimetype="application/xml")

@app.get("/manifest.json")
def pwa_manifest():
    return send_from_directory(PUBLIC, "manifest.json", mimetype="application/manifest+json")

@app.get("/sw.js")
def pwa_service_worker():
    response = send_from_directory(PUBLIC, "sw.js", mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response

@app.get("/icons/<path:filename>")
def pwa_icons(filename):
    return send_from_directory(PUBLIC + "/icons", filename)

@app.get("/googleac94f63de24e8b6f.html")
def google_verification():
    return send_from_directory(PUBLIC, "googleac94f63de24e8b6f.html", mimetype="text/html")

@app.get("/founding")
def founding_page():
    # Retired founding-member offer (per-booking pricing) — superseded by €179/€299 flat.
    return redirect("/", code=302)


@app.get("/admin/replies")
def admin_replies_page():
    """Reply dashboard: templates + stats για γρήγορη απάντηση σε prospects."""
    return send_from_directory(PUBLIC, "admin_replies.html")

@app.get("/pricing")
def pricing_page():
    return send_from_directory(PUBLIC, "pricing.html")

@app.get("/how-it-works")
def how_it_works_page():
    # Retired page (€99-setup activation flow) — the live funnel is 14d trial via /#pricing.
    return redirect("/", code=302)

@app.get("/about")
def about_page():
    return send_from_directory(PUBLIC, "about.html")

@app.get("/compare")
def compare_page():
    """Comparison page vs all Greek competitors."""
    resp = send_from_directory(PUBLIC, "compare.html")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp

# (removed) duplicate /en route — the canonical /en above serves index_en.html,
# whose pricing was realigned to the live €179/€299 flat + 14d-trial model.

@app.get("/calculator")
def calculator_page():
    return send_from_directory(PUBLIC, "calculator.html")

@app.get("/thank-you")
def thank_you_page():
    return send_from_directory(PUBLIC, "thank-you.html")

@app.get("/academy")
def academy_page():
    """Academy: πώς το στήνει & πώς δουλεύει ο γιατρός (onboarding/SEO)."""
    resp = send_from_directory(PUBLIC, "academy.html")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp

FOUNDING_TEST_EMAILS = ("owner@example.com",)

@app.get("/api/founding/spots")
def founding_spots():
    """Returns remaining founding member spots (excluding test/owner submissions)."""
    try:
        import database as db
        taken = db.count_founding_members(exclude_emails=FOUNDING_TEST_EMAILS)
        total = 5
        remaining = max(0, total - taken)
        return jsonify({"taken": taken, "remaining": remaining, "total": total})
    except Exception as e:
        return jsonify({"taken": 0, "remaining": 5, "total": 5, "error": str(e)})


@app.post("/api/onboarding/submit")
def onboarding_submit():
    """Receives business info form from /thank-you page after Stripe payment.
    Saves to founding_members table + sends emails to founder + customer."""
    try:
        from email_service import send_email_via_resend
        import database as db
        data = request.get_json(silent=True, force=True) or {}

        biz_name = (data.get("business_name") or "").strip()[:200]
        biz_type = (data.get("business_type") or "").strip()[:80]
        owner    = (data.get("owner_name") or "").strip()[:120]
        phone    = (data.get("phone") or "").strip()[:40]
        email    = (data.get("email") or "").strip()[:200]
        address  = (data.get("address") or "").strip()[:300]
        services = (data.get("services") or "").strip()[:3000]
        hours    = (data.get("hours") or "").strip()[:1000]
        emerg    = (data.get("emergency_handling") or "").strip()[:300]
        notes    = (data.get("special_notes") or "").strip()[:2000]

        if not biz_name or not owner or not email:
            return jsonify({"error": "missing required fields"}), 400

        # Save to DB (counts as founding member spot if not test email)
        try:
            db.insert_founding_member({
                "business_name": biz_name, "business_type": biz_type,
                "owner_name": owner, "email": email, "phone": phone,
                "address": address, "services": services, "hours": hours,
                "emergency_handling": emerg, "special_notes": notes,
            })
        except Exception as db_err:
            print(f"[ONBOARDING DB ERROR] {db_err}", flush=True)

        html = f"""<h2>🆕 Νέα onboarding submission</h2>
<table cellpadding="6" style="font-family:Arial;font-size:14px;border-collapse:collapse;">
<tr><td><b>Επωνυμία:</b></td><td>{biz_name}</td></tr>
<tr><td><b>Τύπος:</b></td><td>{biz_type}</td></tr>
<tr><td><b>Ιδιοκτήτης:</b></td><td>{owner}</td></tr>
<tr><td><b>Email:</b></td><td><a href="mailto:{email}">{email}</a></td></tr>
<tr><td><b>Τηλέφωνο:</b></td><td><a href="tel:{phone}">{phone}</a></td></tr>
<tr><td><b>Διεύθυνση:</b></td><td>{address}</td></tr>
<tr><td valign="top"><b>Υπηρεσίες:</b></td><td><pre style="white-space:pre-wrap;font-family:inherit;margin:0;">{services}</pre></td></tr>
<tr><td valign="top"><b>Ωράριο:</b></td><td><pre style="white-space:pre-wrap;font-family:inherit;margin:0;">{hours}</pre></td></tr>
<tr><td><b>Επείγοντα:</b></td><td>{emerg}</td></tr>
<tr><td valign="top"><b>Σημειώσεις:</b></td><td><pre style="white-space:pre-wrap;font-family:inherit;margin:0;">{notes}</pre></td></tr>
</table>
<p style="margin-top:20px;color:#666;font-size:13px;">Submitted: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>"""

        send_email_via_resend(
            to_email="owner@example.com",
            subject=f"🆕 Νέος πελάτης: {biz_name} ({biz_type})",
            html=html,
            from_name="Ran-Tevu Onboarding",
        )

        # Β) AUTO-SETUP: στήσε ΑΜΕΣΩΣ live το ιατρείο (Vapi φωνή + back office) προσαρμοσμένο
        # στα ΔΙΚΑ ΤΟΥ στοιχεία — αντί χειροκίνητο setup 24-48h. Idempotent (σταθερό slug).
        live_links = {}
        try:
            import provisioning
            from voice.onboarding import slugify
            res = provisioning.provision(
                biz_name, biz_type or "default", owner_name=owner, email=email,
                phone=phone, address=address, demo=False, slug=slugify(biz_name))
            slug_p = res.get("slug")
            live_links = res.get("links", {}) or {}
            # προσαρμογή φωνής/back-office στα δικά του: τηλέφωνο (για μεταφορά) + υπηρεσίες/ώρες/επείγοντα
            cfg = {}
            if phone:
                cfg["phone"] = phone
            if email:
                cfg["owner_email"] = email
            ci = []
            if services: ci.append(f"Υπηρεσίες & τιμές: {services}")
            if hours:    ci.append(f"Ωράριο: {hours}")
            if emerg:    ci.append(f"Επείγοντα: {emerg}")
            if notes:    ci.append(f"Οδηγίες ιδιοκτήτη: {notes}")
            if ci:
                cfg["voice_custom_instructions"] = ("\n".join(ci))[:2000]
            if cfg and slug_p:
                db.update_business(slug_p, cfg)
        except Exception as prov_err:
            print(f"[ONBOARDING PROVISION ERROR] {prov_err}", flush=True)

        # confirmation στον πελάτη — ΠΛΗΡΗΣ welcome+οδηγός (κοινό template) αν στήθηκε live
        if email and live_links:
            try:
                from routes.billing_routes import _send_welcome_email
                _send_welcome_email(email, biz_name, live_links, owner_name=owner)
            except Exception as we:
                print(f"[ONBOARDING welcome] {we}")
        elif email:
            send_email_via_resend(
                to_email=email,
                subject=f"Λάβαμε την εγγραφή σου — {biz_name}",
                html=(f"<p>Καλωσόρισες {owner}! Λάβαμε τα στοιχεία του <b>{biz_name}</b> "
                      f"και στήνουμε το σύστημά σου — θα έχεις τα στοιχεία σου πολύ σύντομα.</p>"
                      f"<p>— Κωνσταντίνος, Ran-Tevu</p>"),
                from_name="Κωνσταντίνος (Ran-Tevu)",
            )

        return jsonify({"ok": True}), 200
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.get("/api/admin/email-playbook")
def admin_email_playbook():
    """Στέλνει στον Ντίνο (gmail) το πλήρες sales playbook: τι πουλάμε, κόστος,
    ανταγωνισμός, 4-Stage Closer takeaways, discovery. ?token=ADMIN_TOKEN"""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        from email_service import send_email_via_resend
        to = os.environ.get("OUTREACH_OWNER_EMAIL", "owner@example.com")
        S = "font-family:Arial,Helvetica,sans-serif;color:#1a1d24;"
        H = lambda t: f'<h2 style="{S}font-size:18px;margin:26px 0 10px;border-left:4px solid #6d8bff;padding-left:10px;">{t}</h2>'
        html = f"""<div style="{S}max-width:640px;margin:0 auto;font-size:15px;line-height:1.6;">
<h1 style="{S}font-size:24px;">Ran-Tevu — Sales Playbook 🎯</h1>
<p style="color:#667;">Τι πουλάμε, πόσο μας κοστίζει, τι κάνει ο ανταγωνισμός, και πώς κλείνεις (4-Stage Closer). Όλα σε ένα.</p>

{H('1) Τι πουλάμε — σε μία πρόταση')}
<p><b>«Σου γυρίζω πίσω τα λεφτά από no-shows και χαμένους ασθενείς»</b> — όχι «AI που σηκώνει το τηλέφωνο» (αυτό το λένε 7 άλλοι, ίδια τιμή).</p>
<p><b>Τα 3 που πουλάνε πιο δυνατά:</b></p>
<ul>
<li>Χάνεις 4 στις 10 κλήσεις → ο ασθενής πάει στον διπλανό (85% δεν ξανακαλεί, 62% παίρνει ανταγωνιστή)</li>
<li>Σου φέρνω πίσω no-shows & χαμένους ασθενείς — κανένας Έλληνας δεν το κάνει</li>
<li>14 μέρες δωρεάν, χωρίς κάρτα — αν δεν πιάσει ασθενή, φεύγεις</li>
</ul>

{H('2) Κόστος ανά πελάτη (COGS)')}
<table style="{S}border-collapse:collapse;width:100%;font-size:14px;">
<tr style="background:#f3f5fb;"><th style="text-align:left;padding:8px;border:1px solid #e2e6f0;">Χρήση</th><th style="padding:8px;border:1px solid #e2e6f0;">Κόστος/μήνα</th><th style="padding:8px;border:1px solid #e2e6f0;">Κέρδος @€179</th></tr>
<tr><td style="padding:8px;border:1px solid #e2e6f0;">Χαμηλή (~50 κλήσεις)</td><td style="padding:8px;border:1px solid #e2e6f0;text-align:center;">€36</td><td style="padding:8px;border:1px solid #e2e6f0;text-align:center;"><b>€113 (76%)</b></td></tr>
<tr><td style="padding:8px;border:1px solid #e2e6f0;">Μεσαία (~90 κλήσεις)</td><td style="padding:8px;border:1px solid #e2e6f0;text-align:center;">€60</td><td style="padding:8px;border:1px solid #e2e6f0;text-align:center;"><b>€89 (60%)</b></td></tr>
<tr><td style="padding:8px;border:1px solid #e2e6f0;">Υψηλή (~150 κλήσεις)</td><td style="padding:8px;border:1px solid #e2e6f0;text-align:center;">€89</td><td style="padding:8px;border:1px solid #e2e6f0;text-align:center;"><b>€60 (40%)</b></td></tr>
</table>
<p>Κύριο κόστος = η φωνή (~€0.26/λεπτό). <b>Στους 10 πελάτες (μεσαία):</b> έσοδα €1.490 − κόστος €598 = <b>κέρδος €892/μήνα (~€10.700/χρόνο)</b>. Ρίσκο: τεράστιος όγκος κλήσεων ρίχνει το margin → βάλε fair-use cap (500'/μήνα) αν χρειαστεί.</p>

{H('3) Ανταγωνισμός — η αλήθεια')}
<p>Το «AI που σηκώνει τηλέφωνο» στην Ελλάδα είναι <b>γεμάτο</b>: VoithosAI (€149, physio, inbound-only), Orfeas (€30), AIcallcenter (€99-399), Yuboto/Voice Logica, Φοίβη AI. <b>Το κενό σου:</b> συστηματικό no-show recovery + reactivation, vertical-locked σε physio, ROI dashboard, 14 μέρες δωρεάν χωρίς κάρτα (κανείς δεν το δίνει — όλοι setup fee €199-799). Διεθνώς αποδεδειγμένο: Assort $102M, Lassie $47M (a16z). Πούλα recovery, όχι receptionist.</p>

{H('4) Τι αποκομίζω από το 4-Stage Closer (PDF)')}
<p><b>Είσαι Stage 1→2.</b> Αξιοποίησε αυτά τα 4:</p>
<p><b>① Belief Diagnostic</b> — ο γιατρός κλείνει μόνο αν πιστέψει 7 πράγματα. Κρίσιμα: Problem (απόδειξέ το: «4/10 κλήσεις χάνονται»), Solution (recording+demo), Offer (14 μέρες δωρεάν). Αν κολλήσει → βρες ΠΟΙΟ belief έσπασε, μη σπρώχνεις.</p>
<p><b>② Conversation Architecture</b> (η σειρά):<br>
1. Άνοιξε με τον κόσμο ΤΟΥ: «Πώς κλείνεις ραντεβού τώρα;»<br>
2. Βρες το πραγματικό πρόβλημα: «Όταν χτυπάει ενώ είσαι με ασθενή, τι γίνεται;»<br>
3. Τέντωσε το χάσμα (οικονομικά): «Πόσους νέους χάνεις/μήνα × €300 LTV;»<br>
4. Άσ' τον να πουλήσει μόνος: «Γιατί ψάχνεις λύση τώρα;»<br>
5. Παρουσίασε τη λύση — 3 πυλώνες, &lt;10% της κουβέντας.</p>
<p><b>③ Silence Weapon</b> — μετά από κάθε ερώτηση, ΣΚΑΣΕ, μέτρα ως το 10. Ο γιατρός γεμίζει τη σιωπή με την αλήθεια.</p>
<p><b>④ DAC αντιρρήσεις</b> (Diffuse→Agree→Collaborate):<br>
• «€179 ακριβό» → «Όταν λες ακριβό τι εννοείς;» → «1 χαμένος ασθενής=€300, πληρώνεις €179»<br>
• «Δεν έχω χρόνο» → «Στήνεται μόνο του, 14 μέρες δωρεάν να το δεις»<br>
• «Να το σκεφτώ» → «Τι σε προβληματίζει — η τιμή ή αν θα δουλέψει;»</p>
<p><b>⑤ Conviction</b> — «Το close rate σου δεν ξεπερνά ποτέ την πεποίθησή σου.» Έχεις tested προϊόν (70 tests + review + live). Πες το €179 χωρίς να τρέμει η φωνή σου.</p>

{H('5) Discovery — ρώτα αυτά για να δεις αν έχει ήδη λύση')}
<ul>
<li>«Ποιος σου σηκώνει το τηλέφωνο όταν δουλεύεις πάνω σε ασθενή;»</li>
<li>«Έχεις γραμματέα; Τι ώρες; Μετά τις 5, το Σάββατο;» (41% κλήσεων εκτός 8-5)</li>
<li>«Έχεις doctoranytime/app; Πόσοι το χρησιμοποιούν αντί τηλέφωνο;» (80% κλείνουν ΑΚΟΜΑ τηλεφωνικά)</li>
<li>«Πόσα no-shows τη βδομάδα;»</li>
</ul>
<p><b>Αν έχει ήδη κάτι:</b> ΠΟΤΕ «πέτα το». Πάντα «συμπλήρωμα»: «Τη γραμματέα την κρατάς — εγώ δουλεύω όταν ΕΚΕΙΝΗ δεν μπορεί».</p>

{H('6) Επόμενο βήμα')}
<p>1) Κάνε το demo recording (κάλεσε τον αριθμό ή web call από Vapi).<br>
2) Βάλε το RANTEVU_ENC_KEY στο Railway.<br>
3) <b>Σήκωσε το τηλέφωνο — 30 cold touches σε physios με recovery angle.</b> Το προϊόν είναι έτοιμο· το μόνο που λείπει είσαι εσύ στο τηλέφωνο.</p>

<p style="color:#999;font-size:12px;margin-top:30px;">Ran-Tevu · αυτόματο email playbook · {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
</div>"""
        sent = send_email_via_resend(to_email=to, subject="🎯 Ran-Tevu — Sales Playbook (κόστος + ανταγωνισμός + 4-Stage Closer)",
                                     html=html, from_name="Ran-Tevu Playbook")
        return jsonify({"sent": bool(sent), "to": to})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)[:300]}), 200


@app.get("/api/admin/email-content")
def admin_email_content():
    """Στέλνει στο gmail του Ντίνου το εβδομαδιαίο content (content/week-posts-v2.md),
    έτοιμο για Buffer. ?token=ADMIN_TOKEN"""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        from email_service import send_email_via_resend
        sel = request.args.get("file", "")
        if sel == "reels":
            fname, subj = "reels-v1.md", "🎬 Ran-Tevu — Reel Scripts (έτοιμα να γυρίσεις)"
        elif sel == "daily":
            fname, subj = "daily-15jun.md", "📋 Ran-Tevu — Τι έγινε σήμερα (15/6)"
        elif sel.replace("-", "").replace("_", "").isalnum() and sel.endswith(".md"):
            fname, subj = sel, f"Ran-Tevu — {sel}"
        else:
            fname, subj = "week-posts-v2.md", "📅 Ran-Tevu — Content εβδομάδας (έτοιμο για Buffer)"
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "content", fname)
        if not os.path.exists(path):
            return jsonify({"error": "content file not found"}), 404
        raw = open(path, encoding="utf-8").read()
        try:
            import markdown as _md
            body = _md.markdown(raw, extensions=["nl2br"])
        except Exception:
            body = "<pre style='white-space:pre-wrap'>" + raw.replace("<", "&lt;") + "</pre>"
        html = ('<div style="font-family:Arial,Helvetica,sans-serif;max-width:680px;margin:0 auto;'
                'font-size:15px;line-height:1.6;color:#1a1d24;">' + body + "</div>")
        to = os.environ.get("OUTREACH_OWNER_EMAIL", "owner@example.com")
        sent = send_email_via_resend(to_email=to, subject=subj, html=html, from_name="Ran-Tevu Content")
        return jsonify({"sent": bool(sent), "to": to, "chars": len(raw)})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)[:300]}), 200


@app.post("/api/admin/cold/enqueue")
def admin_cold_enqueue():
    """Βάζει personalized emails στην ουρά. Body: {emails:[{email,name,subject,body}]}. ?token=ADMIN_TOKEN"""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        import cold_outreach
        emails = (request.get_json(silent=True, force=True) or {}).get("emails", [])
        return jsonify(cold_outreach.enqueue(emails))
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)[:300]}), 200


@app.get("/api/admin/cold/run")
def admin_cold_run():
    """Manual trigger του daily batch. ?n=25&token=ADMIN_TOKEN"""
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        import cold_outreach
        n = int(request.args.get("n", "25"))
        return jsonify(cold_outreach.send_batch(limit=n))
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)[:300]}), 200


@app.get("/api/admin/cold/stats")
def admin_cold_stats():
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not _admin_ok(token):
        return jsonify({"error": "unauthorized"}), 401
    try:
        import cold_outreach
        return jsonify(cold_outreach.stats())
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 200


@app.get("/voice-sample-dermatology.mp3")
def voice_sample_derm():
    return send_from_directory(PUBLIC, "voice-sample-dermatology.mp3", mimetype="audio/mpeg")

@app.get("/voice-sample-dermatology-v2.mp3")
def voice_sample_derm_v2():
    return send_from_directory(PUBLIC, "voice-sample-dermatology-v2.mp3", mimetype="audio/mpeg")


@app.get("/privacy")
def privacy_page():
    return send_from_directory(PUBLIC, "privacy.html")

@app.get("/terms")
def terms_page():
    return send_from_directory(PUBLIC, "terms.html")

@app.get("/dpa")
def dpa_page():
    """Art.28 GDPR Data Processing Agreement — signed before any patient data enters (pilot kit step 1)."""
    return send_from_directory(PUBLIC, "dpa.html")

@app.get("/unsubscribe")
def unsubscribe_page():
    return send_from_directory(PUBLIC, "unsubscribe.html")

@app.get("/admin/campaign")
def admin_campaign_page():
    return send_from_directory(PUBLIC, "campaign.html")


@app.get("/admin")
@app.get("/admin/")
def admin_master():
    return send_from_directory(PUBLIC, "admin_master.html")

# ── Slug aliases (παλιά URLs → νέα) ─────────────────────────────────────────
SLUG_ALIASES = {
    "demo": "mitropoleos",
}

def _no_cache(resp):
    """Απαγορεύει στον browser να κρατά cached την HTML — πάντα φρέσκο."""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/<slug>")
def chat_page(slug):
    if slug in SLUG_ALIASES:
        return redirect(f"/{SLUG_ALIASES[slug]}", code=301)
    # αν υπάρχει static file με αυτό το όνομα, σέρβιρέ το
    import os as _os
    static_path = _os.path.join(PUBLIC, slug)
    if _os.path.isfile(static_path):
        return send_from_directory(PUBLIC, slug)
    business = get_business(slug)
    if not business:
        abort(404)
    return _no_cache(send_from_directory(PUBLIC, "chat.html"))

@app.get("/admin/<slug>")
def admin_page(slug):
    if slug in SLUG_ALIASES:
        return redirect(f"/admin/{SLUG_ALIASES[slug]}", code=301)
    business = get_business(slug)
    if not business:
        abort(404)
    return _no_cache(send_from_directory(PUBLIC, "admin.html"))

@app.get("/<path:filename>")
def static_files(filename):
    return send_from_directory(PUBLIC, filename)


if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"  Frontdesk AI — Multi-tenant")
    print(f"  Master Admin: http://localhost:5001/admin")
    print(f"{'='*50}\n")
    app.run(debug=True, port=5001, use_reloader=False)
