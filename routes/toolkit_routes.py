"""
Admin "toolkit" — 4 power-tool endpoints behind the unified dashboard:
- Lead Generation (search by city+industry → reuses scraper code)
- Pipeline (Kanban over outreach_leads.status)
- Email Campaigns (template-based composer + send)
- Google Calendar OAuth scaffolding (πρέπει user να βάλει credentials)
"""
from flask import Blueprint, request, jsonify, send_from_directory, abort
from functools import wraps
import os, json
from datetime import date, datetime, timedelta
import database as db

_PUBLIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public")

bp = Blueprint("toolkit", __name__)


def require_master(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        token = (
            request.headers.get("Authorization", "").replace("Bearer ", "").strip()
            or request.args.get("token", "").strip()
        )
        expected = os.environ.get("ADMIN_TOKEN", "")
        if not token or token != expected:
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrap


# ═══════════════════════════════════════════════════════════════════════
#  LEAD GENERATION
# ═══════════════════════════════════════════════════════════════════════

@bp.post("/api/admin/leads/search")
@require_master
def leads_search():
    """Searches existing leads in outreach_leads OR triggers fresh scrape.
    Body: {industry, city, country, limit}"""
    data = request.get_json(silent=True) or {}
    industry = (data.get("industry") or "").lower().strip()
    city = (data.get("city") or "").lower().strip()
    country = (data.get("country") or "").upper().strip()
    limit = int(data.get("limit", 50))

    where = ["1=1"]
    params = []
    if industry:
        where.append("LOWER(industry) LIKE ?")
        params.append(f"%{industry}%")
    if city:
        where.append("(LOWER(address) LIKE ? OR LOWER(city) LIKE ?)")
        params.extend([f"%{city}%", f"%{city}%"])
    if country:
        where.append("UPPER(country) = ?")
        params.append(country)
    params.append(limit)

    with db._conn() as c:
        # Check if city column exists in outreach_leads
        cols = [r[1] for r in c.execute("PRAGMA table_info(outreach_leads)").fetchall()]
        # Rebuild where without city column if missing
        if "city" not in cols and city:
            where = [w for w in where if "city" not in w]
            where.append("LOWER(address) LIKE ?")
            params = [p for p in params if not p.startswith(f"%{city}")]
            params = params[:-1] + [f"%{city}%", limit]

        try:
            rows = c.execute(f"""
                SELECT id, business_name, email, phone, website,
                       industry, status, address, score, created_at
                FROM outreach_leads
                WHERE {' AND '.join(where)}
                ORDER BY score DESC, created_at DESC
                LIMIT ?
            """, params).fetchall()
        except Exception as e:
            return jsonify({"error": str(e), "results": []})

    return jsonify({
        "results": [dict(r) for r in rows],
        "count": len(rows),
        "filters": {"industry": industry, "city": city, "country": country},
    })


@bp.post("/api/admin/leads/<int:lead_id>/move")
@require_master
def lead_move_stage(lead_id):
    """Move a lead to a different pipeline stage."""
    data = request.get_json(silent=True) or {}
    new_status = (data.get("status") or "").strip()
    if new_status not in ("new", "contacted", "replied", "won", "lost", "low_quality"):
        return jsonify({"error": "invalid status"}), 400

    with db._conn() as c:
        c.execute("UPDATE outreach_leads SET status=? WHERE id=?", (new_status, lead_id))
        c.commit()
    return jsonify({"ok": True, "lead_id": lead_id, "new_status": new_status})


@bp.get("/admin/leads")
def leads_ui():
    return send_from_directory(_PUBLIC_DIR, "admin_leads.html")


# ═══════════════════════════════════════════════════════════════════════
#  PIPELINE (Kanban view over outreach_leads)
# ═══════════════════════════════════════════════════════════════════════

@bp.get("/api/admin/pipeline")
@require_master
def pipeline_data():
    """Returns leads grouped by status (kanban columns)."""
    stages = ["new", "contacted", "replied", "won", "lost"]
    columns = {}
    with db._conn() as c:
        for stage in stages:
            try:
                rows = c.execute("""
                    SELECT id, business_name, email, phone, industry, score, created_at
                    FROM outreach_leads
                    WHERE status=?
                    ORDER BY score DESC, created_at DESC
                    LIMIT 50
                """, (stage,)).fetchall()
                columns[stage] = [dict(r) for r in rows]
            except Exception:
                columns[stage] = []
    return jsonify({"columns": columns})


@bp.get("/admin/pipeline")
def pipeline_ui():
    return send_from_directory(_PUBLIC_DIR, "admin_pipeline.html")


# ═══════════════════════════════════════════════════════════════════════
#  EMAIL CAMPAIGNS
# ═══════════════════════════════════════════════════════════════════════

CAMPAIGN_TEMPLATES = {
    "welcome": {
        "name": "Welcome new client",
        "subject": "Καλώς ήρθατε στο {business_name}! 🎉",
        "body": """Καλημέρα {client_name},

Χαρά μας που σας έχουμε στην οικογένειά μας στο {business_name}!

Το πρώτο σας ραντεβού είναι: {first_appointment}

Αν χρειαστείτε οτιδήποτε, απαντήστε σε αυτό το email ή πάρτε μας {phone}.

— {business_name} Team
""",
    },
    "reminder": {
        "name": "Reminder ραντεβού (24h πριν)",
        "subject": "Υπενθύμιση: Το ραντεβού σας αύριο στις {time}",
        "body": """Καλημέρα {client_name},

Σας υπενθυμίζουμε ότι αύριο έχετε ραντεβού για {service} στις {time}.

Διεύθυνση: {address}

Αν χρειαστεί να ακυρώσετε, πάρτε μας τουλάχιστον 4 ώρες πριν.

— {business_name}
""",
    },
    "reactivation": {
        "name": "Επανενεργοποίηση παλιού πελάτη",
        "subject": "Σας έχουμε χάσει — μήπως ώρα για επίσκεψη;",
        "body": """Καλημέρα {client_name},

Έχει περάσει καιρός από την τελευταία σας επίσκεψη ({last_visit_date}). Σας έχουμε χάσει!

Για να επιστρέψετε, σας προσφέρουμε **20% έκπτωση** στο επόμενο ραντεβού σας.

Κωδικός: WELCOMEBACK20 (έγκυρος μέχρι {expiry})

Πατήστε εδώ για να κλείσετε: {booking_url}

— {business_name}
""",
    },
    "review_request": {
        "name": "Review request μετά από επίσκεψη",
        "subject": "Πώς ήταν η εμπειρία σας στο {business_name};",
        "body": """Καλημέρα {client_name},

Ευχαριστούμε για την πρόσφατη επίσκεψή σας!

Θα μας βοηθούσε πολύ μια **σύντομη κριτική στο Google**:
{google_review_url}

Παίρνει λιγότερο από 60 δευτερόλεπτα και βοηθάει πραγματικά μικρές επιχειρήσεις σαν εμάς.

— {business_name}
""",
    },
    "special_offer": {
        "name": "Ειδική προσφορά / Promo",
        "subject": "{special_subject}",
        "body": """Καλημέρα {client_name},

Έχουμε ξεχωριστή προσφορά για εσάς:

{offer_description}

Διάρκεια: μέχρι {expiry}

Κλείστε εδώ: {booking_url}

— {business_name}
""",
    },
}


@bp.get("/api/admin/campaigns/templates")
@require_master
def campaign_templates():
    return jsonify(CAMPAIGN_TEMPLATES)


@bp.post("/api/admin/campaigns/<slug>/preview")
@require_master
def campaign_preview(slug):
    """Preview rendered campaign for a given audience selector + template."""
    data = request.get_json(silent=True) or {}
    template_id = data.get("template_id", "welcome")
    template = CAMPAIGN_TEMPLATES.get(template_id, CAMPAIGN_TEMPLATES["welcome"])
    business = db.get_business(slug)
    if not business:
        return jsonify({"error": "business not found"}), 404

    # Substitute base placeholders
    subs = {
        "business_name": business.get("name", ""),
        "phone": business.get("phone", ""),
        "address": business.get("address", ""),
        "client_name": "Δοκιμή Πελάτη",
        "first_appointment": "Παρασκευή 22 Μαΐου, 14:00",
        "time": "14:00", "service": "Καθαρισμός",
        "last_visit_date": "Φεβρουάριος 2026",
        "expiry": "30 Ιουνίου 2026",
        "booking_url": f"https://www.ran-tevu.gr/{slug}",
        "google_review_url": business.get("google_review_url") or "https://g.co/review-link",
        "special_subject": data.get("special_subject", "Ειδική προσφορά"),
        "offer_description": data.get("offer_description", "Εκπτωτικό πακέτο"),
    }
    rendered_subject = template["subject"]
    rendered_body = template["body"]
    for k, v in subs.items():
        rendered_subject = rendered_subject.replace("{" + k + "}", str(v))
        rendered_body = rendered_body.replace("{" + k + "}", str(v))

    return jsonify({
        "subject": rendered_subject,
        "body": rendered_body,
        "estimated_recipients": _estimate_audience(slug, data.get("audience", "all_clients")),
    })


def _estimate_audience(slug: str, audience: str) -> int:
    """How many recipients για το επιλεγμένο audience."""
    with db._conn() as c:
        if audience == "all_clients":
            r = c.execute("SELECT COUNT(DISTINCT email) AS n FROM client_profiles WHERE slug=? AND email IS NOT NULL AND email != ''", (slug,)).fetchone()
            return int(r["n"]) if r else 0
        if audience == "inactive_30d":
            cutoff = (date.today() - timedelta(days=30)).isoformat()
            r = c.execute("SELECT COUNT(DISTINCT email) AS n FROM client_profiles WHERE slug=? AND email IS NOT NULL AND (last_visit IS NULL OR last_visit < ?)", (slug, cutoff)).fetchone()
            return int(r["n"]) if r else 0
        if audience == "recent_bookings":
            cutoff = (date.today() - timedelta(days=7)).isoformat()
            r = c.execute("SELECT COUNT(DISTINCT email) AS n FROM bookings WHERE slug=? AND date >= ? AND email IS NOT NULL", (slug, cutoff)).fetchone()
            return int(r["n"]) if r else 0
    return 0


@bp.post("/api/admin/campaigns/<slug>/send")
@require_master
def campaign_send(slug):
    """Sends the campaign. Returns count of emails queued."""
    data = request.get_json(silent=True) or {}
    template_id = data.get("template_id", "welcome")
    audience = data.get("audience", "all_clients")
    custom_subs = data.get("substitutions", {})

    business = db.get_business(slug)
    if not business:
        return jsonify({"error": "business not found"}), 404

    template = CAMPAIGN_TEMPLATES.get(template_id)
    if not template:
        return jsonify({"error": "invalid template"}), 400

    # Resolve recipients
    recipients = _resolve_recipients(slug, audience)
    if not recipients:
        return jsonify({"ok": False, "error": "no recipients", "audience": audience})

    # Send via email_service
    sent = 0
    failed = 0
    errors = []
    try:
        from email_service import send_email
    except ImportError:
        return jsonify({"error": "email_service not available"}), 500

    for r_data in recipients[:200]:  # hard cap per send
        subject = template["subject"]
        body = template["body"]
        subs = {
            "business_name": business.get("name", ""),
            "phone": business.get("phone", ""),
            "address": business.get("address", ""),
            "client_name": r_data.get("name") or "Πελάτη",
            "booking_url": f"https://www.ran-tevu.gr/{slug}",
            "google_review_url": business.get("google_review_url") or "",
            **custom_subs,
        }
        for k, v in subs.items():
            subject = subject.replace("{" + k + "}", str(v))
            body = body.replace("{" + k + "}", str(v))
        try:
            send_email(to=r_data["email"], subject=subject, text=body, html=f"<pre style='font-family:system-ui,sans-serif;white-space:pre-wrap'>{body}</pre>")
            sent += 1
        except Exception as e:
            failed += 1
            errors.append(f"{r_data['email']}: {str(e)[:80]}")
            if len(errors) > 5:
                break

    return jsonify({
        "ok": True,
        "audience": audience,
        "template": template_id,
        "sent": sent,
        "failed": failed,
        "total_recipients": len(recipients),
        "errors": errors[:5],
    })


def _resolve_recipients(slug: str, audience: str) -> list:
    with db._conn() as c:
        if audience == "all_clients":
            rows = c.execute("SELECT DISTINCT email, name FROM client_profiles WHERE slug=? AND email IS NOT NULL AND email != ''", (slug,)).fetchall()
        elif audience == "inactive_30d":
            cutoff = (date.today() - timedelta(days=30)).isoformat()
            rows = c.execute("SELECT DISTINCT email, name FROM client_profiles WHERE slug=? AND email IS NOT NULL AND email != '' AND (last_visit IS NULL OR last_visit < ?)", (slug, cutoff)).fetchall()
        elif audience == "recent_bookings":
            cutoff = (date.today() - timedelta(days=7)).isoformat()
            rows = c.execute("SELECT DISTINCT email, name FROM bookings WHERE slug=? AND date >= ? AND email IS NOT NULL AND email != ''", (slug, cutoff)).fetchall()
        else:
            return []
    return [dict(r) for r in rows]


@bp.get("/admin/<slug>/campaigns")
def campaigns_ui(slug):
    business = db.get_business(slug)
    if not business:
        abort(404)
    return send_from_directory(_PUBLIC_DIR, "admin_campaigns.html")


# ═══════════════════════════════════════════════════════════════════════
#  GOOGLE CALENDAR (OAuth scaffolding)
# ═══════════════════════════════════════════════════════════════════════

def _env(*names) -> bool:
    """True μόνο αν ΟΛΑ τα env vars είναι set & μη-κενά."""
    return all(bool(os.environ.get(n, "").strip()) for n in names)


def _any_env(*names) -> bool:
    return any(bool(os.environ.get(n, "").strip()) for n in names)


def _readiness_report() -> dict:
    """Πλήρης πίνακας ετοιμότητας-πώλησης: κάθε κλειδί (πράσινο/κόκκινο) + τι ξεκλειδώνει
    για τον γιατρό + πώς μπαίνει. Πηγή αλήθειας = ο 48-agent readiness audit (8/7)."""
    import database as _db
    on_railway = bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_ENVIRONMENT"))
    db_path = getattr(_db, "DB", "") or ""
    db_persistent = (not on_railway) or any(db_path.startswith(p) for p in ("/data", "/mnt", "/var/lib")) \
        or ("RAILWAY_VOLUME_MOUNT_PATH" in os.environ)

    items = [
        # ── ΒΑΣΙΚΑ (υποχρεωτικά για να πουλήσεις καθόλου) ──────────────────
        {"key": "anthropic", "group": "core", "icon": "🧠",
         "label": "AI εγκέφαλος (κρατήσεις + γραμματέας)",
         "unlocks": "Ο ασθενής γράφει στο chat κι ο AI κλείνει το ραντεβού· ο γιατρός ρωτάει «ποιος μου χρωστάει;». "
                    "ΧΩΡΙΣ ΑΥΤΟ ΤΙΠΟΤΑ δεν κλείνει — μη δείξεις καθόλου το προϊόν.",
         "configured": _env("ANTHROPIC_API_KEY"), "env": ["ANTHROPIC_API_KEY"],
         "get_url": "https://console.anthropic.com/",
         "how": "console.anthropic.com → API Keys → Create key → βάλε credit στον λογαριασμό → Railway → Variables → ANTHROPIC_API_KEY"},
        {"key": "encryption", "group": "core", "icon": "🔐",
         "label": "Κλειδί κρυπτογράφησης ιατρικών δεδομένων",
         "unlocks": "Κρυπτογραφεί σημειώσεις/διαγνώσεις (GDPR). ΣΤΟ RAILWAY ΧΩΡΙΣ ΑΥΤΟ ΤΟ APP ΔΕΝ ΞΕΚΙΝΑΕΙ (fail-closed).",
         "configured": _env("RANTEVU_ENC_KEY"), "env": ["RANTEVU_ENC_KEY"], "get_url": "",
         "how": "Τρέξε τοπικά: python -c \"from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())\" → "
                "αντίγραψε το → Railway → RANTEVU_ENC_KEY. ⚠️ ΜΗΝ το αλλάξεις/χάσεις ποτέ (αλλιώς χάνονται τα κρυπτογραφημένα)."},
        {"key": "email", "group": "core", "icon": "✉️",
         "label": "Email (επιβεβαιώσεις + υπενθυμίσεις)",
         "unlocks": "Στέλνει στον ασθενή email επιβεβαίωσης & υπενθύμισης (24h/4h) και στον γιατρό ειδοποιήσεις.",
         "configured": _env("RESEND_API_KEY"), "env": ["RESEND_API_KEY"],
         "get_url": "https://resend.com/api-keys",
         "how": "resend.com → API Keys → βεβαιώσου ότι το domain ran-tevu.gr είναι verified → Railway → RESEND_API_KEY"},
        {"key": "admin", "group": "core", "icon": "🔑",
         "label": "Κωδικός διαχειριστή + κλειδί για links γιατρών",
         "unlocks": "Το δικό σου admin login + τα ιδιωτικά links κάθε γιατρού. Χωρίς αυτά κλειδώνεσαι έξω / τα links γιατρών βγάζουν 403.",
         "configured": _env("ADMIN_TOKEN") and _any_env("SECRET_KEY", "DASH_LINK_SECRET"),
         "env": ["ADMIN_TOKEN", "SECRET_KEY"], "get_url": "",
         "how": "Railway → ADMIN_TOKEN=<μεγάλος τυχαίος κωδικός> και SECRET_KEY=<άλλος τυχαίος>. Κράτησέ τους κάπου ασφαλή."},
        {"key": "db", "group": "core", "icon": "💾",
         "label": "Μόνιμη αποθήκευση δεδομένων (persistent volume)",
         "unlocks": "Κρατάει bookings/ασθενείς μετά από κάθε deploy. ΧΩΡΙΣ ΑΥΤΟ κάθε restart ΣΒΗΝΕΙ όλα τα δεδομένα των πελατών σου.",
         "configured": db_persistent, "env": ["DB_PATH"], "get_url": "",
         "how": "Railway → Add Volume (mount path /data) → env DB_PATH=/data/bookings.db"},
        # ── ΓΙΑ ΝΑ ΠΛΗΡΩΝΕΣΑΙ ──────────────────────────────────────────────
        {"key": "stripe", "group": "billing", "icon": "💳",
         "label": "Stripe — για να πληρώνεσαι εσύ",
         "unlocks": "14 μέρες δωρεάν χωρίς κάρτα → €179/€299 τον μήνα αυτόματα. Αυτά είναι τα λεφτά ΣΟΥ.",
         "configured": _env("STRIPE_SECRET_KEY") and _env("STRIPE_WEBHOOK_SECRET"),
         "env": ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"], "get_url": "https://dashboard.stripe.com/apikeys",
         "how": "Stripe → API keys (sk_live) + Webhooks → Add endpoint www.ran-tevu.gr/webhooks/stripe → "
                "Railway → STRIPE_SECRET_KEY + STRIPE_WEBHOOK_SECRET (το webhook secret ξεχωριστά)."},
        # ── ΕΠΙΠΛΕΟΝ ΚΑΝΑΛΙΑ (προαιρετικά — αυξάνουν την αξία) ─────────────
        {"key": "twilio", "group": "channel", "icon": "📱",
         "label": "SMS υπενθυμίσεις (Twilio)",
         "unlocks": "Υπενθυμίσεις με SMS στο κινητό του ασθενή. Χωρίς αυτό στέλνονται ΜΟΝΟ email — μη λες «SMS».",
         "configured": _env("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
         "env": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_DEFAULT_FROM"], "get_url": "https://console.twilio.com/",
         "how": "twilio.com → Account SID + Auth Token + αγόρασε/επιβεβαίωσε νούμερο → Railway → "
                "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_DEFAULT_FROM"},
        {"key": "voice", "group": "channel", "icon": "🎙️",
         "label": "AI φωνητικές κλήσεις (Vapi + ElevenLabs)",
         "unlocks": "Η AI ΠΑΙΡΝΕΙ ΤΗΛΕΦΩΝΟ ασθενείς (no-show/win-back). Χωρίς όλα → καμία κλήση — μη λες «η AI καλεί».",
         "configured": _env("VAPI_API_KEY") and _env("ELEVENLABS_API_KEY"),
         "env": ["VAPI_API_KEY", "ELEVENLABS_API_KEY", "TWILIO_ACCOUNT_SID"], "get_url": "https://vapi.ai/",
         "how": "vapi.ai → VAPI_API_KEY (+VAPI_PUBLIC_KEY) · elevenlabs.io → ELEVENLABS_API_KEY · χρειάζεται και νούμερο Twilio."},
        {"key": "whatsapp", "group": "channel", "icon": "💬",
         "label": "WhatsApp / Instagram inbox",
         "unlocks": "Λαμβάνει DM & ο AI γράφει απάντηση. Το ΣΤΕΙΛΙΜΟ πίσω θέλει Meta. Χωρίς → «AI γράφει, στέλνεις εσύ».",
         "configured": _env("META_WHATSAPP_TOKEN", "META_WHATSAPP_PHONE_ID"),
         "env": ["META_WHATSAPP_TOKEN", "META_WHATSAPP_PHONE_ID", "META_VERIFY_TOKEN", "META_INSTAGRAM_USER_ID"],
         "get_url": "https://developers.facebook.com/",
         "how": "Meta Business + WhatsApp Business Platform (μέσω BSP π.χ. Twilio/360dialog) → Railway → "
                "META_WHATSAPP_TOKEN, META_WHATSAPP_PHONE_ID, META_VERIFY_TOKEN, META_INSTAGRAM_USER_ID"},
        {"key": "gcal", "group": "channel", "icon": "📅",
         "label": "Google Calendar sync (προαιρετικό)",
         "unlocks": "2-way sync ραντεβού με το Google Calendar του γιατρού.",
         "configured": _env("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"),
         "env": ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"], "get_url": "https://console.cloud.google.com/apis/credentials",
         "how": "Google Cloud Console → OAuth Client (Web) → redirect www.ran-tevu.gr/api/integrations/google/callback → "
                "Railway → GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET"},
    ]

    core = [i for i in items if i["group"] == "core"]
    core_ready = sum(1 for i in core if i["configured"])
    works = []
    if _env("ANTHROPIC_API_KEY"):
        works.append("Κρατήσεις μέσω chat — ο ασθενής κλείνει ραντεβού μόνος του")
        works.append("Back-office: ημέρα, φάκελοι ασθενών, τιμολόγια + ΦΠΑ, «ποιος μου χρωστάει»")
    if _env("RESEND_API_KEY"):
        works.append("Email επιβεβαίωσης + υπενθυμίσεις 24h/4h στον ασθενή")
    works.append("Προστασία διπλο-κλεισίματος + απόλυτη απομόνωση δεδομένων ανά γιατρό (πάντα ενεργά)")

    return {
        "on_railway": on_railway,
        "summary": {
            "core_ready": core_ready, "core_total": len(core),
            "sellable": core_ready == len(core),
            "works_now": works,
        },
        "items": items,
    }


@bp.get("/api/admin/integrations/status")
@require_master
def integrations_status():
    """Πίνακας ετοιμότητας-πώλησης: όλα τα keys (πράσινο/κόκκινο) + τι ξεκλειδώνει + πώς μπαίνει.
    Κρατάει και τα legacy flat πεδία για backward-compat με παλιά UI."""
    rep = _readiness_report()
    cfg = {i["key"]: i["configured"] for i in rep["items"]}
    return jsonify({
        # legacy flat (backward-compat)
        "google_calendar": {
            "configured": cfg.get("gcal", False),
            "setup_url": "https://console.cloud.google.com/apis/credentials",
            "scopes_needed": ["https://www.googleapis.com/auth/calendar.events"],
        },
        "google_calendar_oauth_redirect": f"{os.environ.get('PUBLIC_BASE_URL', 'https://www.ran-tevu.gr')}/api/integrations/google/callback",
        "vapi": {"configured": bool(os.environ.get("VAPI_API_KEY"))},
        "twilio": {"configured": bool(os.environ.get("TWILIO_ACCOUNT_SID"))},
        "elevenlabs": {"configured": bool(os.environ.get("ELEVENLABS_API_KEY"))},
        "stripe": {"configured": bool(os.environ.get("STRIPE_SECRET_KEY"))},
        "resend": {"configured": bool(os.environ.get("RESEND_API_KEY"))},
        # rich readiness cockpit
        "readiness": rep,
    })


@bp.get("/api/integrations/google/authorize/<slug>")
@require_master
def google_calendar_authorize(slug):
    """Redirect user to Google OAuth consent screen.
    Πρέπει user να έχει set GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET στο env."""
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        return jsonify({
            "error": "Google OAuth not configured",
            "setup_steps": [
                "1. Πήγαινε https://console.cloud.google.com/apis/credentials",
                "2. Create OAuth client (Web app)",
                "3. Authorized redirect URI: https://www.ran-tevu.gr/api/integrations/google/callback",
                "4. Add to Railway env: GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET",
            ],
        }), 400

    from urllib.parse import urlencode
    redirect_uri = f"{os.environ.get('PUBLIC_BASE_URL', 'https://www.ran-tevu.gr')}/api/integrations/google/callback"
    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/calendar.events",
        "access_type": "offline",
        "prompt": "consent",
        "state": slug,  # so callback knows which business
    }
    return jsonify({
        "authorize_url": f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(auth_params)}",
    })


@bp.get("/api/integrations/google/callback")
def google_calendar_callback():
    """Exchange code for tokens. Stores refresh_token in businesses table.
    Δεν χρειάζεται @require_master γιατί έρχεται από Google redirect."""
    code = request.args.get("code", "")
    slug = request.args.get("state", "")
    if not code or not slug:
        return "Missing code or state", 400

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        return "OAuth not configured server-side", 500

    import requests
    redirect_uri = f"{os.environ.get('PUBLIC_BASE_URL', 'https://www.ran-tevu.gr')}/api/integrations/google/callback"
    try:
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }, timeout=15)
        tokens = r.json()
        if "access_token" not in tokens:
            return f"Token exchange failed: {tokens}", 400

        # Store refresh_token in DB (add column if missing)
        with db._conn() as c:
            try:
                c.execute("ALTER TABLE businesses ADD COLUMN google_refresh_token TEXT DEFAULT ''")
            except Exception:
                pass
            c.execute("UPDATE businesses SET google_refresh_token=? WHERE slug=?",
                      (tokens.get("refresh_token", ""), slug))
            c.commit()

        return f"""<html><body style='font-family:system-ui;padding:40px;text-align:center;background:#0a0a0f;color:white'>
        <h1>✅ Google Calendar συνδέθηκε!</h1>
        <p>Τα ραντεβού σας θα συγχρονίζονται με το Google Calendar σας.</p>
        <a href='/admin/{slug}/dashboard?token=' style='color:#fbbf24'>← Πίσω στο dashboard</a>
        </body></html>""", 200

    except Exception as e:
        return f"OAuth error: {e}", 500


@bp.get("/admin/integrations")
def integrations_ui():
    return send_from_directory(_PUBLIC_DIR, "admin_integrations.html")


@bp.get("/admin/hub")
@bp.get("/hub")
def admin_hub_ui():
    """Μία πόρτα για όλο το προϊόν: κουμπιά για ασθενή/γιατρό/admin σελίδες."""
    return send_from_directory(_PUBLIC_DIR, "hub.html")


@bp.get("/api/hub/access")
def hub_access_status():
    """Απλό status για το UI: δείχνει αν το token έχει περάσει και αν το owner είναι ενεργό."""
    token = (request.headers.get("Authorization", "").replace("Bearer ", "").strip()
             or request.args.get("token", "").strip())
    expected = os.environ.get("ADMIN_TOKEN", "")
    owner = False
    try:
        from routes.start_routes import is_owner
        owner = bool(is_owner())
    except Exception:
        owner = False
    return jsonify({
        "token_ok": bool(expected) and bool(token) and token == expected,
        "owner_ok": owner,
        "ready": bool(expected) and (bool(token) and token == expected or owner),
    })


def register(app):
    app.register_blueprint(bp)
