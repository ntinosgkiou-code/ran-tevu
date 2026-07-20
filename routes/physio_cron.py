"""
Physio Email Campaign — Railway cron endpoint
POST /cron/physio-emails   Bearer <ADMIN_TOKEN>  (set in Railway env)
Στέλνει 12 emails/μέρα σε φυσιοθεραπευτές από scraper/physio_leads.csv
"""
import os, csv, json, time, re, urllib.request, urllib.error
from flask import Blueprint, request, jsonify
from database import _conn

bp = Blueprint("physio_cron", __name__)

ADMIN_TOKEN    = os.environ.get("ADMIN_TOKEN", "")
RESEND_KEY     = os.environ.get("RESEND_API_KEY", "")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
DAILY_LIMIT    = 12

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEADS_CSV = os.path.join(BASE_DIR, "scraper", "physio_leads.csv")

SYSTEM_PROMPT = """Είσαι expert sales copywriter για B2B SaaS στην Ελλάδα. Γράφεις cold emails για ΦΥΣΙΟΘΕΡΑΠΕΥΤΕΣ.

ΤΟ ANGLE (Revenue Recovery — ΟΧΙ "AI που σηκώνει τηλέφωνο", αυτό το λένε όλοι):
Ο φυσιοθεραπευτής χάνει αόρατο χρήμα από: (α) ασθενείς που σταμάτησαν τη θεραπεία στη μέση και χάθηκαν, (β) ραντεβού που ακυρώθηκαν/no-show και κανείς δεν τους ξανακάλεσε, (γ) κενά slots που έμειναν νεκρά. Κανείς ανταγωνιστής δεν τα κυνηγάει αυτά.

Το προϊόν: Ran-Tevu (ran-tevu.gr) — κυνηγάει ΑΥΤΟΜΑΤΑ τους χαμένους/no-show ασθενείς και τους ξαναφέρνει, γεμίζει τα κενά, και δείχνει live πόσα € σου γύρισε. (Σηκώνει και το τηλέφωνο 24/7, αλλά αυτό είναι bonus.) 14 μέρες δωρεάν χωρίς κάρτα, μετά €179/μήνα.

Core one-liner: "Reminder προλαμβάνει. Recovery επανακτά."

Κανόνες:
1. ΑΝΟΙΓΜΑ με το pain που κανείς δεν τους έχει πει: "Πόσοι ασθενείς σου σταμάτησαν τη θεραπεία στη μέση τον τελευταίο μήνα;" (ή variant). Αναφέρει 1 specific detail από website αν υπάρχει.
2. 6-8 γραμμές MAX, χωρίς bullets
3. Subject 45 chars MAX, χωρίς spam words — γύρω από "χαμένους ασθενείς" / "ραντεβού που χάθηκαν"
4. Casual, ανθρώπινος τόνος
5. ROI frame: "1 ασθενής που γυρίζει = ένα πακέτο συνεδριών (€240-500). Το εργαλείο €179/μήνα."
6. CTA: "Αν σε ενδιαφέρει, απάντησε με ένα 'ναι' και σου το στήνω δωρεάν 14 μέρες."
7. ΟΧΙ "Σας γράφω γιατί...", ΟΧΙ formal Greek. ΟΧΙ ιατρικές παροτρύνσεις ("πρέπει να συνεχίσει τη θεραπεία") — μόνο "θες να τον ξανακλείσεις;" (booking-only, όχι medical advice).

Signature: — Κωνσταντίνος\n📞 6987747434\n🌐 ran-tevu.gr

Επιστρέφεις μόνο JSON: {"subject": "...", "body": "..."}"""


def _ensure_table():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS physio_sent (
                email TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            )
        """)


def _already_sent(email):
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM physio_sent WHERE email=?", (email,)).fetchone()
        return row is not None


def _mark_sent(email):
    from datetime import datetime
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO physio_sent (email, sent_at) VALUES (?,?)",
                     (email, datetime.utcnow().isoformat()))


def _claude_email(name, city, website):
    context = f"Φυσιοθεραπευτής: {name}\nΠόλη: {city}\nWebsite: {website or 'δεν έχει'}"
    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 500,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": context}]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read())
            text = data["content"][0]["text"]
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                parsed = json.loads(m.group())
                return parsed.get("subject", ""), parsed.get("body", "")
    except Exception as e:
        print(f"[PHYSIO_CRON] Claude error: {e}")
    return None, None


def _send_resend(to_email, subject, body):
    body_html = f"""<!DOCTYPE html><html lang="el"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 16px"><tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0"
  style="background:#fff;border-radius:12px;padding:36px 40px;border:1px solid #e5e7eb">
<tr><td>
<div style="font-size:15px;color:#374151;line-height:1.75;white-space:pre-wrap">{body}</div>
<hr style="border:none;border-top:1px solid #f3f4f6;margin:24px 0">
<div style="font-size:12px;color:#9ca3af">Αν δεν θέλεις να λαμβάνεις, απάντησε "διαγραφή".</div>
</td></tr></table></td></tr></table></body></html>"""

    payload = json.dumps({
        "from": "Κωνσταντίνος <noreply@ran-tevu.gr>",
        "to": [to_email],
        "reply_to": "ntinosgkiou@gmail.com",
        "subject": subject,
        "text": body,
        "html": body_html,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={"Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
                 "Accept": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except urllib.error.HTTPError as e:
        print(f"[PHYSIO_CRON] Resend error {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        print(f"[PHYSIO_CRON] Resend exception: {e}")
    return False


@bp.route("/cron/physio-emails", methods=["POST", "GET"])
def physio_emails_cron():
    auth = request.headers.get("Authorization", "")
    if not ADMIN_TOKEN or auth != f"Bearer {ADMIN_TOKEN}":
        return jsonify({"error": "unauthorized"}), 403

    _ensure_table()

    if not os.path.exists(LEADS_CSV):
        return jsonify({"error": "leads CSV not found", "path": LEADS_CSV}), 500

    # Load leads with email
    leads = []
    with open(LEADS_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            email = row.get("email", "").strip().lower()
            if email and "@" in email:
                leads.append(row)

    # Filter unsent
    unsent = [l for l in leads if not _already_sent(l["email"].strip().lower())]
    batch  = unsent[:DAILY_LIMIT]

    if not batch:
        return jsonify({"status": "done", "message": "all leads already sent", "total_sent": len(leads) - len(unsent)})

    sent_ok, sent_fail = 0, 0
    for lead in batch:
        email   = lead["email"].strip().lower()
        name    = lead.get("name", "")
        city    = lead.get("city", "")
        website = lead.get("website", "")

        subject, body = _claude_email(name, city, website)
        if not subject or not body:
            sent_fail += 1
            continue

        ok = _send_resend(email, subject, body)
        if ok:
            _mark_sent(email)
            sent_ok += 1
        else:
            sent_fail += 1

        time.sleep(2)

    remaining = len(unsent) - len(batch)
    return jsonify({
        "status": "ok",
        "sent": sent_ok,
        "failed": sent_fail,
        "remaining": remaining,
        "days_left": max(0, (remaining // DAILY_LIMIT))
    })


def register(app):
    app.register_blueprint(bp)
    print("[PHYSIO_CRON] blueprint loaded — POST /cron/physio-emails")
