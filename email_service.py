"""
Email service — Resend (primary, zero config) + Gmail SMTP (fallback per business)

Resend:  set RESEND_API_KEY in Railway env vars → emails work for ALL businesses immediately
Gmail:   optional per-business override via smtp_email + smtp_password in settings
"""

import os
import json
import smtplib
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

RESEND_API_KEY       = os.environ.get("RESEND_API_KEY", "")
FROM_NAME            = os.environ.get("FROM_NAME", "Σύστημα Ραντεβού")
FROM_DOMAIN          = os.environ.get("FROM_DOMAIN", "")   # e.g. "yourdomain.gr"
# Platform-level Gmail SMTP fallback (set in Railway → Variables)
# Accept both SMTP_EMAIL (code) and SMTP_USER (documented in .env.example) so the
# documented template doesn't silently leave the fallback disabled.
GLOBAL_SMTP_EMAIL    = os.environ.get("SMTP_EMAIL") or os.environ.get("SMTP_USER", "")
GLOBAL_SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

# Sender address: "Name <noreply@yourdomain.gr>" or resend.dev shared domain
def _from_addr() -> str:
    if FROM_DOMAIN:
        return f"{FROM_NAME} <noreply@{FROM_DOMAIN}>"
    return f"{FROM_NAME} <onboarding@resend.dev>"


def _base_url() -> str:
    # Prefer the BRANDED domain (www.ran-tevu.gr) for patient-facing links; fall back to the raw
    # *.up.railway.app host only if no branded URL is set. Unifies with billing/voice conventions.
    domain = (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("BASE_URL")
              or os.environ.get("RAILWAY_PUBLIC_DOMAIN"))
    if domain:
        return f"https://{domain}" if not domain.startswith("http") else domain
    return "http://localhost:5001"


def _gcal_link(b: dict, business: dict) -> str:
    start  = datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M")
    end    = start + timedelta(minutes=b.get("duration", 60))
    fmt    = "%Y%m%dT%H%M%S"
    params = {
        "action":   "TEMPLATE",
        "text":     f"{b['service']} — {business['name']}",
        "dates":    f"{start.strftime(fmt)}/{end.strftime(fmt)}",
        "details":  f"Ραντεβού: {b['service']}\nΠελάτης: {b['name']}\nΤηλ: {business['phone']}",
        "location": business.get("address", ""),
    }
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)


def _smtp_ok(business: dict) -> bool:
    return bool(business.get("smtp_email") and business.get("smtp_password"))

def _global_smtp_ok() -> bool:
    return bool(GLOBAL_SMTP_EMAIL and GLOBAL_SMTP_PASSWORD)


# ── Core send functions ───────────────────────────────────────────────────────

def _send_via_resend(to: str, subject: str, html: str) -> bool:
    """Send via Resend API. Returns True on success."""
    if not RESEND_API_KEY:
        return False
    payload = json.dumps({
        "from":    _from_addr(),
        "to":      [to],
        "subject": subject,
        "html":    html,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type":  "application/json",
            # ΧΩΡΙΣ User-Agent το Cloudflare του Resend μπλοκάρει με 1010 → δεν φεύγει ΚΑΝΕΝΑ email
            "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
            "Accept":        "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = r.status in (200, 201)
            if ok:
                print(f"[RESEND ✅] → {to}")
            return ok
    except Exception as e:
        try:
            from debug_log import log_error
            log_error(f"_send_via_resend(to={to})", e)
        except Exception:
            print(f"[RESEND ERROR] {type(e).__name__}: {e}")
        return False


def _send_via_smtp(business: dict, to: str, subject: str, html: str):
    """Send via business's own Gmail SMTP (port 587 STARTTLS)."""
    msg            = MIMEMultipart("alternative")
    msg["From"]    = business["smtp_email"]
    msg["To"]      = to
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(business["smtp_email"], business["smtp_password"])
        server.sendmail(business["smtp_email"], to, msg.as_string())
    print(f"[SMTP ✅] → {to}")


def _send_via_global_smtp(to: str, subject: str, html: str):
    """Send via platform-level Gmail (SMTP_EMAIL / SMTP_PASSWORD Railway vars, port 587 STARTTLS)."""
    msg            = MIMEMultipart("alternative")
    msg["From"]    = f"{FROM_NAME} <{GLOBAL_SMTP_EMAIL}>"
    msg["To"]      = to
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(GLOBAL_SMTP_EMAIL, GLOBAL_SMTP_PASSWORD)
        server.sendmail(GLOBAL_SMTP_EMAIL, to, msg.as_string())
    print(f"[GLOBAL SMTP ✅] → {to}")


def _send_html(business: dict, to: str, subject: str, html: str) -> bool:
    """Try Resend → per-business SMTP → platform SMTP → log skip.
    Επιστρέφει True ΜΟΝΟ αν έφυγε πραγματικά από ένα κανάλι (αλλιώς False — κανένα fake-success)."""
    if _send_via_resend(to, subject, html):
        return True
    if _smtp_ok(business):
        try:
            _send_via_smtp(business, to, subject, html)
            return True
        except Exception as e:
            print(f"[SMTP ERROR] {e}")
    if _global_smtp_ok():
        try:
            _send_via_global_smtp(to, subject, html)
            return True
        except Exception as e:
            print(f"[GLOBAL SMTP ERROR] {e}")
    print(f"[EMAIL SKIP] No email method configured for {business.get('slug','?')} → {to}")
    return False


# ── Email templates ───────────────────────────────────────────────────────────

def _accent(business: dict) -> str:
    return business.get("accent_color") or "#7C3AED"

def _grad(business: dict) -> str:
    c = _accent(business)
    return f"linear-gradient(135deg,{c},{c}dd)"

def _footer(business: dict) -> str:
    return f"{business['name']} · {business.get('address','')} · {business.get('phone','')}"


def send_confirmation(booking: dict, business: dict):
    try:
        _send_to_client(booking, business)
        _send_to_owner(booking, business)
    except Exception as e:
        print(f"[CONFIRMATION ERROR] {e}")


def _send_to_client(b: dict, business: dict):
    if not b.get("email"):
        return
    gcal     = _gcal_link(b, business)
    from app import _booking_token   # signed link — the endpoint 403s without a valid token
    ical_url = f"{_base_url()}/api/ical/{b['id']}?t={_booking_token(b['id'], 'ical')}"
    grad     = _grad(business)
    subject  = f"✅ Επιβεβαίωση ραντεβού — {business['name']}"
    html = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/></head>
<body style="margin:0;padding:0;background:#f3f4f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:36px 16px;">
<tr><td align="center">
<table width="540" cellpadding="0" cellspacing="0"
  style="background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.10);">
  <tr><td style="background:{grad};padding:30px 36px;">
    <p style="margin:0;font-size:13px;color:rgba(255,255,255,0.7);text-transform:uppercase;letter-spacing:.08em;">Επιβεβαίωση Ραντεβού</p>
    <p style="margin:6px 0 0;font-size:26px;font-weight:800;color:#fff;">{business['name']}</p>
  </td></tr>
  <tr><td style="padding:32px 36px 24px;">
    <p style="font-size:16px;font-weight:600;color:#111827;margin:0 0 6px;">Γεια σου, {b['name']}! 👋</p>
    <p style="font-size:14px;color:#6b7280;margin:0 0 28px;">Το ραντεβού σου <strong>επιβεβαιώθηκε</strong> επιτυχώς.</p>
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;border-radius:16px;">
      <tr><td style="padding:20px 22px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr><td style="padding:8px 0;font-size:13px;color:#9ca3af;width:120px;">📅 Ημερομηνία</td>
              <td style="padding:8px 0;font-size:14px;font-weight:700;color:#111827;">{b['date']}</td></tr>
          <tr><td style="padding:8px 0;font-size:13px;color:#9ca3af;border-top:1px solid #f0f0f0;">🕐 Ώρα</td>
              <td style="padding:8px 0;font-size:14px;font-weight:700;color:#111827;border-top:1px solid #f0f0f0;">{b['time']}</td></tr>
          <tr><td style="padding:8px 0;font-size:13px;color:#9ca3af;border-top:1px solid #f0f0f0;">💆 Υπηρεσία</td>
              <td style="padding:8px 0;font-size:14px;font-weight:700;color:#111827;border-top:1px solid #f0f0f0;">{b['service']}</td></tr>
          <tr><td style="padding:8px 0;font-size:13px;color:#9ca3af;border-top:1px solid #f0f0f0;">📍 Διεύθυνση</td>
              <td style="padding:8px 0;font-size:14px;color:#374151;border-top:1px solid #f0f0f0;">{business.get('address','')}</td></tr>
        </table>
      </td></tr>
    </table>
    <table cellpadding="0" cellspacing="0" style="margin-top:24px;">
      <tr>
        <td style="padding-right:10px;">
          <a href="{gcal}" style="display:inline-block;background:{grad};color:#fff;text-decoration:none;padding:13px 22px;border-radius:28px;font-size:13px;font-weight:600;">📅 Google Calendar</a>
        </td>
        <td>
          <a href="{ical_url}" style="display:inline-block;background:#f3f4f8;color:#374151;text-decoration:none;padding:13px 22px;border-radius:28px;font-size:13px;font-weight:600;">📲 Λήψη .ics</a>
        </td>
      </tr>
    </table>
    <p style="font-size:13px;color:#9ca3af;margin:28px 0 0;border-top:1px solid #f5f5f5;padding-top:20px;">
      Για ακύρωση: <a href="tel:{business.get('phone','')}" style="color:{_accent(business)};font-weight:600;">{business.get('phone','')}</a>
    </p>
  </td></tr>
  <tr><td style="background:#f9fafb;border-top:1px solid #f0f0f0;padding:16px 36px;text-align:center;">
    <p style="margin:0;font-size:12px;color:#9ca3af;">{_footer(business)}</p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""
    _send_html(business=business, to=b["email"], subject=subject, html=html)


def _send_to_owner(b: dict, business: dict):
    if not business.get("owner_email"):
        return
    gcal    = _gcal_link(b, business)
    grad    = _grad(business)
    subject = f"🆕 Νέο ραντεβού — {b['name']} ({b['date']} {b['time']})"
    html = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="utf-8"/></head>
<body style="margin:0;padding:24px;background:#f3f4f8;font-family:-apple-system,sans-serif;">
<div style="max-width:480px;background:#fff;border-radius:18px;padding:28px;box-shadow:0 4px 24px rgba(0,0,0,0.08);">
  <p style="margin:0 0 20px;font-size:16px;font-weight:700;color:#111827;">🆕 Νέο Ραντεβού — {business['name']}</p>
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:13px;color:#9ca3af;width:110px;">👤 Πελάτης</td>
        <td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:14px;font-weight:600;">{b['name']}</td></tr>
    <tr><td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:13px;color:#9ca3af;">📞 Τηλέφωνο</td>
        <td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:14px;">{b['phone']}</td></tr>
    <tr><td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:13px;color:#9ca3af;">📧 Email</td>
        <td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:14px;">{b.get('email','—')}</td></tr>
    <tr><td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:13px;color:#9ca3af;">📅 Ημερομηνία</td>
        <td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:14px;font-weight:700;color:#111827;">{b['date']}</td></tr>
    <tr><td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:13px;color:#9ca3af;">🕐 Ώρα</td>
        <td style="padding:9px 0;border-bottom:1px solid #f5f5f5;font-size:14px;font-weight:700;color:#111827;">{b['time']}</td></tr>
    <tr><td style="padding:9px 0;font-size:13px;color:#9ca3af;">💆 Υπηρεσία</td>
        <td style="padding:9px 0;font-size:14px;">{b['service']} ({b['duration']} λεπτά)</td></tr>
  </table>
  <a href="{gcal}" style="display:inline-block;margin-top:20px;background:{grad};color:#fff;text-decoration:none;padding:12px 22px;border-radius:24px;font-size:13px;font-weight:600;">📅 Πρόσθεσε στο ημερολόγιό σου</a>
</div>
</body></html>"""
    _send_html(business=business, to=business["owner_email"], subject=subject, html=html)


def send_cancellation_to_client(booking: dict, business: dict):
    """Στέλνει ειδοποίηση ακύρωσης στον πελάτη όταν ο owner ακυρώσει."""
    if not booking.get("email"):
        return
    grad    = _grad(business)
    subject = f"❌ Ακύρωση ραντεβού — {business['name']}"
    html = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/></head>
<body style="margin:0;padding:0;background:#f3f4f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:36px 16px;">
<tr><td align="center">
<table width="540" cellpadding="0" cellspacing="0"
  style="background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.10);">
  <tr><td style="background:{grad};padding:30px 36px;">
    <p style="margin:0;font-size:13px;color:rgba(255,255,255,0.7);text-transform:uppercase;">Ακύρωση Ραντεβού</p>
    <p style="margin:6px 0 0;font-size:26px;font-weight:800;color:#fff;">{business['name']}</p>
  </td></tr>
  <tr><td style="padding:32px 36px 28px;">
    <p style="font-size:16px;font-weight:600;color:#111827;margin:0 0 8px;">Γεια σου, {booking['name']},</p>
    <p style="font-size:14px;color:#6b7280;margin:0 0 24px;">
      Το ραντεβού σου για <strong>{booking['date']} στις {booking['time']}</strong>
      (<em>{booking['service']}</em>) <strong>ακυρώθηκε</strong> από την επιχείρηση.
    </p>
    <p style="font-size:14px;color:#374151;margin:0 0 24px;">
      Για να κλείσεις νέο ραντεβού ή για απορίες, επικοινώνησε μαζί μας:
    </p>
    <a href="tel:{business.get('phone','')}"
       style="display:inline-block;background:{grad};color:#fff;text-decoration:none;
              padding:13px 24px;border-radius:28px;font-size:14px;font-weight:600;">
      📞 {business.get('phone','')}
    </a>
    <p style="font-size:12px;color:#9ca3af;margin:28px 0 0;border-top:1px solid #f5f5f5;padding-top:16px;">
      {_footer(business)}
    </p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""
    try:
        _send_html(business=business, to=booking["email"], subject=subject, html=html)
        print(f"[CANCEL EMAIL ✅] → booking #{booking.get('id','?')} [{booking.get('slug','?')}]")
    except Exception as e:
        print(f"[CANCEL EMAIL ERROR] {e}")


def send_setup_confirmation(owner_email: str, owner_name: str, business_name: str,
                             slug: str, admin_token: str, base_url: str):
    """Στέλνει στον νέο client τα credentials του μετά το setup."""
    if not owner_email:
        return
    chat_url  = f"{base_url}/{slug}"
    admin_url = f"{base_url}/admin/{slug}"
    subject   = f"🎉 Ο λογαριασμός σου στο {FROM_NAME} είναι έτοιμος!"
    html = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/></head>
<body style="margin:0;padding:0;background:#f3f4f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:36px 16px;">
<tr><td align="center">
<table width="540" cellpadding="0" cellspacing="0"
  style="background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.10);">
  <tr><td style="background:linear-gradient(135deg,#7C3AED,#4F46E5);padding:30px 36px;">
    <p style="margin:0;font-size:13px;color:rgba(255,255,255,0.7);text-transform:uppercase;">Καλώς ήρθες!</p>
    <p style="margin:6px 0 0;font-size:26px;font-weight:800;color:#fff;">{business_name}</p>
  </td></tr>
  <tr><td style="padding:32px 36px 28px;">
    <p style="font-size:16px;font-weight:600;color:#111827;margin:0 0 8px;">Γεια σου, {owner_name}! 🎉</p>
    <p style="font-size:14px;color:#6b7280;margin:0 0 28px;">
      Ο λογαριασμός σου δημιουργήθηκε επιτυχώς. Αποθήκευσε αυτά τα στοιχεία σε ασφαλές μέρος.
    </p>

    <div style="background:#f9fafb;border-radius:16px;padding:22px;margin-bottom:24px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr><td style="padding:8px 0;font-size:13px;color:#9ca3af;width:140px;">🔗 Chat link</td>
            <td style="padding:8px 0;font-size:13px;font-weight:600;color:#7C3AED;">
              <a href="{chat_url}" style="color:#7C3AED;">{chat_url}</a>
            </td></tr>
        <tr><td style="padding:8px 0;font-size:13px;color:#9ca3af;border-top:1px solid #f0f0f0;">⚙️ Admin panel</td>
            <td style="padding:8px 0;font-size:13px;font-weight:600;color:#7C3AED;border-top:1px solid #f0f0f0;">
              <a href="{admin_url}" style="color:#7C3AED;">{admin_url}</a>
            </td></tr>
        <tr><td style="padding:8px 0;font-size:13px;color:#9ca3af;border-top:1px solid #f0f0f0;">🔑 Admin token</td>
            <td style="padding:8px 0;font-size:13px;border-top:1px solid #f0f0f0;">
              <code style="background:#EDE9FE;color:#5B21B6;padding:3px 8px;border-radius:6px;font-size:12px;letter-spacing:.03em;">{admin_token}</code>
            </td></tr>
      </table>
    </div>

    <p style="font-size:13px;color:#6b7280;margin:0 0 20px;">
      💡 <strong>Βήματα εκκίνησης:</strong><br/>
      1. Άνοιξε το Admin Panel και μπες με το Admin Token<br/>
      2. Μοιράσου το Chat Link με τους πελάτες σου<br/>
      3. Τα ραντεβού θα εμφανίζονται αυτόματα στο admin
    </p>

    <a href="{admin_url}"
       style="display:inline-block;background:linear-gradient(135deg,#7C3AED,#4F46E5);color:#fff;
              text-decoration:none;padding:14px 28px;border-radius:28px;font-size:14px;font-weight:700;">
      ⚙️ Άνοιξε το Admin Panel
    </a>

    <div style="margin-top:28px;padding:20px;background:linear-gradient(135deg,#F3F1FA,#FAFAFB);border-radius:14px;border:1px solid #E6E6EA;">
      <p style="margin:0 0 10px;font-size:14px;font-weight:700;color:#1B1B1F;">✨ Τι έχει ενεργοποιημένο ήδη ο bot σου:</p>
      <ul style="margin:0;padding:0 0 0 18px;font-size:13px;color:#4B4B53;line-height:1.7;">
        <li>💬 <strong>AI Chat 24/7</strong> — instant στο custom link σου</li>
        <li>📞 <strong>Voice receptionist</strong> — provisionαρουμε ελληνικό αριθμό + φωνή εντός 24h</li>
        <li>🧠 <strong>AI Memory</strong> — θυμάται κάθε πελάτη (όνομα, προτιμήσεις) σε επόμενες επισκέψεις</li>
        <li>🇬🇷🇬🇧 <strong>Multi-language</strong> — απαντά αυτόματα Ελληνικά ή Αγγλικά</li>
        <li>📧 <strong>Auto emails</strong> — confirmation αμέσως, reminders 24h + 4h πριν</li>
        <li>📊 <strong>Weekly report</strong> — Δευτέρα 09:00 στο email σου</li>
        <li>🛡️ <strong>EU AI Act + GDPR ready</strong> — full audit logs, consent tracking</li>
        <li>♻️ <strong>Reengagement</strong> — αυτόματα emails σε ανενεργούς πελάτες (Δευτέρα 11:00)</li>
      </ul>
      <p style="margin:14px 0 0;font-size:12px;color:#6B6B73;">
        🔒 Δες τη <a href="https://www.ran-tevu.gr/compliance" style="color:#6E56CF;">compliance &amp; privacy σελίδα</a> για customers που θα ρωτήσουν.
      </p>
    </div>
  </td></tr>
  <tr><td style="background:#f9fafb;border-top:1px solid #f0f0f0;padding:16px 36px;text-align:center;">
    <p style="margin:0;font-size:12px;color:#9ca3af;">Αποθήκευσε αυτό το email — περιέχει τον κωδικό admin σου.</p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""
    try:
        _send_via_resend(owner_email, subject, html) or \
        print(f"[SETUP EMAIL SKIP] No Resend key — token shown on screen only")
        print(f"[SETUP EMAIL ✅] → {owner_email}")
    except Exception as e:
        print(f"[SETUP EMAIL ERROR] {e}")


def send_questionnaire(booking: dict, business: dict, questionnaire_url: str):
    if not booking.get("email"):
        return
    try:
        question = (business.get("questionnaire") or
                    "Έχεις κάτι να μας πεις πριν το ραντεβού σου; (αλλεργίες, ειδικές προτιμήσεις)")
        grad     = _grad(business)
        subject  = f"📋 Πριν το ραντεβού σου — {business['name']}"
        html = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/></head>
<body style="margin:0;padding:0;background:#f3f4f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:36px 16px;">
<tr><td align="center">
<table width="540" cellpadding="0" cellspacing="0"
  style="background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,.10);">
  <tr><td style="background:{grad};padding:28px 36px;">
    <p style="margin:0;font-size:13px;color:rgba(255,255,255,.7);text-transform:uppercase;">Πριν το Ραντεβού</p>
    <p style="margin:6px 0 0;font-size:24px;font-weight:800;color:#fff;">{business['name']}</p>
  </td></tr>
  <tr><td style="padding:32px 36px 28px;">
    <p style="font-size:16px;font-weight:600;color:#111827;margin:0 0 8px;">Γεια σου, {booking.get('name','φίλε')}! 👋</p>
    <p style="font-size:14px;color:#6b7280;margin:0 0 24px;">
      Το ραντεβού σου είναι <strong>{booking['date']} στις {booking['time']}</strong>.<br/>
      Θέλουμε να είμαστε έτοιμοι — μόνο 30 δευτερόλεπτα!
    </p>
    <div style="background:#f9fafb;border-radius:12px;padding:18px 20px;margin-bottom:24px;">
      <p style="font-size:14px;font-weight:600;color:#374151;margin:0;">💬 {question}</p>
    </div>
    <a href="{questionnaire_url}"
       style="display:inline-block;background:{grad};color:#fff;text-decoration:none;
              padding:14px 28px;border-radius:28px;font-size:14px;font-weight:700;">
      ✍️ Απάντησε εδώ (30 δευτ.)
    </a>
    <p style="font-size:12px;color:#9ca3af;margin:20px 0 0;border-top:1px solid #f5f5f5;padding-top:16px;">
      {_footer(business)}
    </p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""
        _send_html(business=business, to=booking["email"], subject=subject, html=html)
        print(f"[QUESTIONNAIRE ✅] → booking #{booking.get('id','?')} [{booking.get('slug','?')}]")
    except Exception as e:
        print(f"[QUESTIONNAIRE ERROR] {e}")


def send_review_request(booking: dict, business: dict):
    review_url = business.get("google_review_url", "")
    if not review_url or not booking.get("email"):
        return
    try:
        grad    = _grad(business)
        subject = f"💬 Πώς ήταν; — {business['name']}"
        html = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/></head>
<body style="margin:0;padding:0;background:#f3f4f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:36px 16px;">
<tr><td align="center">
<table width="540" cellpadding="0" cellspacing="0"
  style="background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,.10);">
  <tr><td style="background:{grad};padding:28px 36px;">
    <p style="margin:0;font-size:13px;color:rgba(255,255,255,.7);text-transform:uppercase;">Σε Ευχαριστούμε!</p>
    <p style="margin:6px 0 0;font-size:24px;font-weight:800;color:#fff;">{business['name']}</p>
  </td></tr>
  <tr><td style="padding:32px 36px 28px;text-align:center;">
    <div style="font-size:48px;margin-bottom:16px;">⭐⭐⭐⭐⭐</div>
    <p style="font-size:18px;font-weight:700;color:#111827;margin:0 0 8px;">Πώς ήταν η εμπειρία σου, {booking.get('name','φίλε')};</p>
    <p style="font-size:14px;color:#6b7280;margin:0 0 28px;">
      Χαρήκαμε που σε είδαμε! Μια κριτική βοηθάει κι άλλους να μας βρουν. 🙏
    </p>
    <a href="{review_url}"
       style="display:inline-block;background:linear-gradient(135deg,#FBBC05,#EA4335);color:#fff;
              text-decoration:none;padding:16px 32px;border-radius:28px;font-size:15px;font-weight:700;">
      ⭐ Άφησε κριτική στο Google
    </a>
    <p style="font-size:12px;color:#9ca3af;margin:28px 0 0;border-top:1px solid #f5f5f5;padding-top:16px;">
      {_footer(business)}
    </p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""
        _send_html(business=business, to=booking["email"], subject=subject, html=html)
        print(f"[REVIEW ✅] → booking #{booking.get('id','?')} [{booking.get('slug','?')}]")
    except Exception as e:
        print(f"[REVIEW ERROR] {e}")


def send_reminder(booking: dict, hours: int, business: dict = None,
                  confirm_url: str = "", cancel_url: str = ""):
    if not booking.get("email"):
        return
    try:
        label  = "24 ώρες" if hours == 24 else "4 ώρες"
        grad   = _grad(business)
        accent = _accent(business)
        subject = f"⏰ Ραντεβού σε {label} — {business['name']}"
        gcal    = _gcal_link(booking, business)

        action_buttons = ""
        if confirm_url and cancel_url:
            action_buttons = f"""
    <div style="margin:24px 0;text-align:center;">
      <p style="font-size:13px;color:#6b7280;margin:0 0 14px;font-weight:500;">Θα έρθεις σίγουρα;</p>
      <table cellpadding="0" cellspacing="0" style="margin:0 auto;">
        <tr>
          <td style="padding-right:10px;">
            <a href="{confirm_url}" style="display:inline-block;background:#10B981;color:#fff;text-decoration:none;padding:13px 24px;border-radius:28px;font-size:14px;font-weight:700;">✅ Ναι, θα είμαι εκεί!</a>
          </td>
          <td>
            <a href="{cancel_url}" style="display:inline-block;background:#f3f4f8;color:#EF4444;text-decoration:none;padding:13px 24px;border-radius:28px;font-size:14px;font-weight:600;border:1.5px solid #EF4444;">❌ Ακύρωσε</a>
          </td>
        </tr>
      </table>
    </div>"""

        html = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/></head>
<body style="margin:0;padding:0;background:#f3f4f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:36px 16px;">
<tr><td align="center">
<table width="540" cellpadding="0" cellspacing="0"
  style="background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.10);">
  <tr><td style="background:{grad};padding:30px 36px;">
    <p style="margin:0;font-size:13px;color:rgba(255,255,255,0.7);text-transform:uppercase;">Υπενθύμιση Ραντεβού</p>
    <p style="margin:6px 0 0;font-size:26px;font-weight:800;color:#fff;">{business['name']}</p>
  </td></tr>
  <tr><td style="padding:32px 36px 24px;">
    <p style="font-size:16px;font-weight:600;color:#111827;margin:0 0 6px;">Γεια σου, {booking['name']}! ⏰</p>
    <p style="font-size:14px;color:#6b7280;margin:0 0 24px;">Το ραντεβού σου είναι σε <strong>{label}</strong>.</p>
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;border-radius:16px;">
      <tr><td style="padding:20px 22px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr><td style="padding:8px 0;font-size:13px;color:#9ca3af;width:120px;">📅 Ημερομηνία</td>
              <td style="padding:8px 0;font-size:14px;font-weight:700;">{booking['date']}</td></tr>
          <tr><td style="padding:8px 0;font-size:13px;color:#9ca3af;border-top:1px solid #f0f0f0;">🕐 Ώρα</td>
              <td style="padding:8px 0;font-size:14px;font-weight:700;border-top:1px solid #f0f0f0;">{booking['time']}</td></tr>
          <tr><td style="padding:8px 0;font-size:13px;color:#9ca3af;border-top:1px solid #f0f0f0;">💆 Υπηρεσία</td>
              <td style="padding:8px 0;font-size:14px;font-weight:700;border-top:1px solid #f0f0f0;">{booking['service']}</td></tr>
          <tr><td style="padding:8px 0;font-size:13px;color:#9ca3af;border-top:1px solid #f0f0f0;">📍 Διεύθυνση</td>
              <td style="padding:8px 0;font-size:14px;color:#374151;border-top:1px solid #f0f0f0;">{business.get('address','')}</td></tr>
        </table>
      </td></tr>
    </table>
    {action_buttons}
    <p style="font-size:13px;color:#9ca3af;margin:20px 0 0;border-top:1px solid #f5f5f5;padding-top:16px;">
      Επικοινωνία: <a href="tel:{business.get('phone','')}" style="color:{accent};font-weight:600;">{business.get('phone','')}</a>
    </p>
  </td></tr>
  <tr><td style="background:#f9fafb;border-top:1px solid #f0f0f0;padding:16px 36px;text-align:center;">
    <p style="margin:0;font-size:12px;color:#9ca3af;">{_footer(business)}</p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""
        ok = _send_html(business=business, to=booking["email"], subject=subject, html=html)
        if ok:
            print(f"[REMINDER ✅] {label} → booking #{booking.get('id','?')} [{business.get('slug','?')}]")
        else:
            print(f"[REMINDER SKIP] {label} → booking #{booking.get('id','?')} — κανένα email κανάλι")
        return ok
    except Exception as e:
        print(f"[REMINDER ERROR] {e}")
        return False


def send_fill_slots_email(client: dict, available_date: str, chat_url: str, business: dict):
    if not client.get("email"):
        return
    try:
        grad    = _grad(business)
        subject = f"📅 Έχουμε ελεύθερη ώρα για σένα — {business['name']}"
        html = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="utf-8"/></head>
<body style="margin:0;padding:0;background:#f3f4f8;font-family:-apple-system,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:36px 16px;"><tr><td align="center">
<table width="540" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.10);">
  <tr><td style="background:{grad};padding:30px 36px;">
    <p style="margin:0;font-size:13px;color:rgba(255,255,255,0.7);text-transform:uppercase;">Ελεύθερη Ώρα</p>
    <p style="margin:6px 0 0;font-size:26px;font-weight:800;color:#fff;">{business['name']}</p>
  </td></tr>
  <tr><td style="padding:32px 36px 24px;">
    <p style="font-size:16px;font-weight:600;color:#111827;margin:0 0 12px;">Γεια σου, {client.get('name','φίλε')}! 👋</p>
    <p style="font-size:15px;color:#374151;margin:0 0 8px;">Έχουμε ελεύθερες ώρες την <strong>{available_date}</strong> — και σκεφτήκαμε εσένα!</p>
    <p style="font-size:14px;color:#6b7280;margin:0 0 28px;">Κλείσε τώρα εύκολα online 👇</p>
    <a href="{chat_url}" style="display:inline-block;background:{grad};color:#fff;text-decoration:none;padding:14px 28px;border-radius:28px;font-size:14px;font-weight:700;">📅 Κλείσε ραντεβού τώρα</a>
    <p style="font-size:13px;color:#9ca3af;margin:28px 0 0;border-top:1px solid #f5f5f5;padding-top:20px;">
      Ή κάλεσέ μας: <a href="tel:{business.get('phone','')}" style="color:{_accent(business)};font-weight:600;">{business.get('phone','')}</a>
    </p>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""
        _send_html(business=business, to=client["email"], subject=subject, html=html)
        print(f"[FILL_SLOTS ✅] → {client.get('name')} ({client['email']})")
    except Exception as e:
        print(f"[FILL_SLOTS ERROR] {e}")


def send_reengagement_email(client: dict, chat_url: str, business: dict):
    if not client.get("email"):
        return
    try:
        grad         = _grad(business)
        last_service = ""
        if client.get("history"):
            last_service = client["history"][0].get("service", "")
        last_str = f" για <strong>{last_service}</strong>" if last_service else ""
        subject  = f"Σε έχουμε να έρθεις, {client.get('name','φίλε')}! — {business['name']}"
        html = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="utf-8"/></head>
<body style="margin:0;padding:0;background:#f3f4f8;font-family:-apple-system,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:36px 16px;"><tr><td align="center">
<table width="540" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.10);">
  <tr><td style="background:{grad};padding:30px 36px;">
    <p style="margin:0;font-size:13px;color:rgba(255,255,255,0.7);text-transform:uppercase;">Σε Έχουμε!</p>
    <p style="margin:6px 0 0;font-size:26px;font-weight:800;color:#fff;">{business['name']}</p>
  </td></tr>
  <tr><td style="padding:32px 36px 24px;">
    <p style="font-size:16px;font-weight:600;color:#111827;margin:0 0 12px;">Γεια σου, {client.get('name','φίλε')}! 💕</p>
    <p style="font-size:15px;color:#374151;margin:0 0 8px;">Η τελευταία φορά που ήρθες{last_str} ήταν εδώ και λίγο καιρό — <strong>σε έχουμε!</strong></p>
    <p style="font-size:14px;color:#6b7280;margin:0 0 28px;">Κλείσε το επόμενο ραντεβού σου εύκολα online 👇</p>
    <a href="{chat_url}" style="display:inline-block;background:{grad};color:#fff;text-decoration:none;padding:14px 28px;border-radius:28px;font-size:14px;font-weight:700;">📅 Κλείσε ραντεβού</a>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""
        _send_html(business=business, to=client["email"], subject=subject, html=html)
        print(f"[REENGAGE ✅] → {client.get('name')} ({client['email']})")
    except Exception as e:
        print(f"[REENGAGE ERROR] {e}")


def send_waitlist_offer(waiter: dict, claim_url: str, business: dict, expires_in_hours: int = 6):
    """Στέλνει στον αναμένοντα email με κουμπί 'Πάρε εσύ τη θέση' (one-click claim).
    Το link έχει HMAC token και λήγει σε X ώρες."""
    if not waiter.get("email"):
        print(f"[WAITLIST] Waiter {waiter.get('name')} χωρίς email — μόνο SMS/owner notify.")
        return False
    try:
        grad    = _grad(business)
        accent  = _accent(business)
        name    = waiter.get("name") or "φίλε"
        service = waiter.get("service", "")
        wdate   = waiter.get("date", "")
        wtime   = waiter.get("time", "")

        subject = f"🎉 Ελευθερώθηκε η θέση σου — {business['name']}"
        html = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="utf-8"/></head>
<body style="margin:0;padding:0;background:#f3f4f8;font-family:-apple-system,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:36px 16px;"><tr><td align="center">
<table width="540" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:22px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.10);">
  <tr><td style="background:{grad};padding:32px 36px;">
    <p style="margin:0;font-size:13px;color:rgba(255,255,255,0.8);text-transform:uppercase;letter-spacing:1px;">Καλά νέα!</p>
    <p style="margin:6px 0 0;font-size:26px;font-weight:800;color:#fff;">Ελευθερώθηκε η θέση σου</p>
  </td></tr>
  <tr><td style="padding:32px 36px 12px;">
    <p style="font-size:16px;font-weight:600;color:#111827;margin:0 0 12px;">Γεια σου, {name}! 🎉</p>
    <p style="font-size:15px;color:#374151;line-height:1.6;margin:0 0 24px;">
      Καλά νέα! Ελευθερώθηκε η θέση που περίμενες στο <strong>{business['name']}</strong>.
    </p>

    <table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;border-radius:14px;padding:18px;margin-bottom:24px;">
      <tr><td style="padding:6px 0;font-size:13px;color:#6b7280;width:90px;">📅 Ημ/νία</td>
          <td style="padding:6px 0;font-size:14px;font-weight:700;color:{accent};">{wdate}</td></tr>
      <tr><td style="padding:6px 0;font-size:13px;color:#6b7280;">🕐 Ώρα</td>
          <td style="padding:6px 0;font-size:14px;font-weight:700;color:{accent};">{wtime}</td></tr>
      <tr><td style="padding:6px 0;font-size:13px;color:#6b7280;">💆 Υπηρεσία</td>
          <td style="padding:6px 0;font-size:14px;font-weight:600;">{service}</td></tr>
    </table>

    <p style="font-size:14px;color:#374151;margin:0 0 18px;">Πάτα το κουμπί παρακάτω για να κλείσεις τη θέση. <strong>Πρώτος που πατάει — πρώτος που κλείνει.</strong></p>

    <a href="{claim_url}" style="display:inline-block;background:{grad};color:#fff;text-decoration:none;padding:14px 32px;border-radius:28px;font-size:15px;font-weight:700;">✅ Πάρε εσύ τη θέση</a>

    <p style="font-size:12px;color:#9ca3af;margin:20px 0 0;">⏱️ Το link ισχύει για {expires_in_hours} ώρες. Αν δεν το πατήσεις, η θέση δίνεται στον επόμενο της λίστας αναμονής.</p>
  </td></tr>
  <tr><td style="background:#f9fafb;padding:18px 36px;border-top:1px solid #f0f0f3;">
    <p style="font-size:12px;color:#9ca3af;margin:0;">{_footer(business)}</p>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""
        _send_html(business=business, to=waiter["email"], subject=subject, html=html)
        print(f"[WAITLIST OFFER ✅] → {name} ({waiter['email']}) για {wdate} {wtime}")
        return True
    except Exception as e:
        print(f"[WAITLIST OFFER ERROR] {e}")
        return False


# ── Owner notifications (human-in-the-loop) ──────────────────────────────────

_OWNER_EVENT_LABELS = {
    "voice_booking":    "🤖 Νέο ραντεβού από AI τηλεφωνήτρια",
    "chat_booking":     "🤖 Νέο ραντεβού από AI chat",
    "voice_cancel":     "❌ Ακύρωση μέσω AI",
    "no_show_detected": "⚠️ Πιθανό no-show",
    "recovery_booked":  "✅ No-show ξανακλείστηκε από AI",
    "waitlist_filled":  "✅ Κενό γέμισε από λίστα αναμονής",
    "outbound_result":  "📞 Αποτέλεσμα κλήσης AI",
}


def _format_date_dmy(date_str: str) -> str:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d/%m")
    except Exception:
        return date_str or ""


def send_owner_notification(business_slug: str, event: dict) -> bool:
    """Ειδοποιεί τον ιδιοκτήτη για κάθε ενέργεια του AI (human-in-the-loop).

    SMS πρώτα (άμεσο), email backup αν λείπει τηλέφωνο ή αποτύχει το SMS.
    Best-effort: δεν σηκώνει ποτέ exception — η ροή booking δεν πρέπει να σπάσει.
    event: {type, customer, phone, date, time, service, detail}
    """
    try:
        import database as db  # lazy — αποφυγή κυκλικού import (db → voice.outbound → εδώ)

        business = db.get_business(business_slug)
        if not business:
            return False

        etype = event.get("type", "")
        label = _OWNER_EVENT_LABELS.get(etype, "🤖 Ενέργεια AI")
        parts = []
        if event.get("customer"):
            parts.append(str(event["customer"]))
        when = " ".join(x for x in (_format_date_dmy(event.get("date", "")), event.get("time", "")) if x)
        if when:
            parts.append(when)
        if event.get("service"):
            parts.append(str(event["service"]))
        if event.get("phone"):
            parts.append(f"τηλ {event['phone']}")
        if event.get("detail"):
            parts.append(str(event["detail"]))
        body = f"{label}: " + " — ".join(parts) if parts else label

        sent = False
        owner_phone = (business.get("phone") or "").strip()
        if owner_phone:
            try:
                from voice.twilio_client import send_sms, is_configured as _twilio_ok
                if _twilio_ok():
                    send_sms(owner_phone, body)
                    sent = True
                    print(f"[OWNER SMS ✅] {etype} → {owner_phone}")
            except Exception as e:
                print(f"[OWNER SMS ERROR] {e}")

        owner_email = (business.get("owner_email") or "").strip()
        if not sent and owner_email:
            try:
                rows = "".join(
                    f'<tr><td style="padding:5px 0;font-size:13px;color:#6b7280;width:100px;">{k}</td>'
                    f'<td style="padding:5px 0;font-size:14px;font-weight:600;color:#111827;">{v}</td></tr>'
                    for k, v in (
                        ("Πελάτης", event.get("customer", "")),
                        ("Ημ/νία",  _format_date_dmy(event.get("date", ""))),
                        ("Ώρα",     event.get("time", "")),
                        ("Υπηρεσία", event.get("service", "")),
                        ("Τηλέφωνο", event.get("phone", "")),
                        ("Λεπτομέρεια", event.get("detail", "")),
                    ) if v
                )
                html = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="utf-8"/></head>
<body style="margin:0;padding:0;background:#f3f4f8;font-family:-apple-system,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:36px 16px;"><tr><td align="center">
<table width="480" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:18px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.10);">
  <tr><td style="padding:26px 30px 8px;">
    <p style="margin:0 0 14px;font-size:18px;font-weight:800;color:#111827;">{label}</p>
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;border-radius:12px;padding:14px;">{rows}</table>
  </td></tr>
  <tr><td style="padding:14px 30px 22px;">
    <p style="font-size:12px;color:#9ca3af;margin:0;">{_footer(business)}</p>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""
                _send_html(business=business, to=owner_email,
                           subject=f"{label} — {business.get('name', '')}", html=html)
                sent = True
                print(f"[OWNER EMAIL ✅] {etype} → {owner_email}")
            except Exception as e:
                print(f"[OWNER EMAIL ERROR] {e}")

        if not sent:
            print(f"[OWNER NOTIFY SKIP] {business_slug}: ούτε τηλέφωνο ούτε owner_email")
        return sent
    except Exception as e:
        print(f"[OWNER NOTIFY ERROR] {e}")
        return False


def send_weekly_report(owner_email: str, business: dict, stats: dict):
    """Εβδομαδιαία αναφορά στον ιδιοκτήτη — κάθε Δευτέρα πρωί."""
    if not owner_email:
        return
    try:
        name       = business.get("name", "Η επιχείρησή σας")
        total      = stats["total"]
        prev       = stats["prev_total"]
        cancelled  = stats["cancelled"]
        revenue    = stats["revenue"]
        new_cl     = stats["new_clients"]
        top_svc    = stats.get("top_service")
        week_start = stats["week_start"]
        week_end   = stats["week_end"]

        # Σύγκριση με προηγούμενη εβδομάδα
        if prev > 0:
            diff = total - prev
            if diff > 0:
                trend = f"<span style='color:#16a34a;font-weight:700;'>↑ +{diff} από την προηγούμενη εβδομάδα</span>"
            elif diff < 0:
                trend = f"<span style='color:#dc2626;font-weight:700;'>↓ {diff} από την προηγούμενη εβδομάδα</span>"
            else:
                trend = f"<span style='color:#6b7280;'>→ Ίδιο με την προηγούμενη εβδομάδα</span>"
        else:
            trend = ""

        top_svc_html = ""
        if top_svc:
            top_svc_html = f"""
            <tr>
              <td style="padding:12px 0;border-bottom:1px solid #f3f4f6;color:#6b7280;font-size:14px;">Δημοφιλέστερη υπηρεσία</td>
              <td style="padding:12px 0;border-bottom:1px solid #f3f4f6;font-weight:600;font-size:14px;text-align:right;">{top_svc['service']} ({top_svc['cnt']}×)</td>
            </tr>"""

        revenue_html = ""
        if revenue > 0:
            revenue_html = f"""
            <tr>
              <td style="padding:12px 0;border-bottom:1px solid #f3f4f6;color:#6b7280;font-size:14px;">Έσοδα εβδομάδας</td>
              <td style="padding:12px 0;border-bottom:1px solid #f3f4f6;font-weight:600;font-size:14px;text-align:right;">€{revenue:.0f}</td>
            </tr>"""

        # Memory stats (verified working feature)
        memory_total     = stats.get("memory_total", 0) or 0
        memory_returning = stats.get("memory_returning", 0) or 0
        memory_html = ""
        if memory_total > 0:
            memory_html = f"""
            <tr>
              <td style="padding:12px 0;border-bottom:1px solid #f3f4f6;color:#6b7280;font-size:14px;">🧠 Πελάτες στη μνήμη του AI</td>
              <td style="padding:12px 0;border-bottom:1px solid #f3f4f6;font-weight:600;font-size:14px;text-align:right;">{memory_total} <span style="color:#6E56CF;font-size:12px;">({memory_returning} επανερχόμενοι)</span></td>
            </tr>"""

        subject = f"📊 Εβδομαδιαία Αναφορά — {name} ({week_start} έως {week_end})"

        if total == 0:
            summary_msg = "Δεν υπήρξαν ραντεβού αυτή την εβδομάδα."
            hero_color  = "#6b7280"
        elif total >= prev and total > 0:
            summary_msg = "Καλή εβδομάδα! Το σύστημά σας δουλεύει. 💪"
            hero_color  = "#7c3aed"
        else:
            summary_msg = "Η εβδομάδα ήταν πιο ήσυχη από συνήθως."
            hero_color  = "#6b7280"

        html = f"""<!DOCTYPE html>
<html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
</head>
<body style="margin:0;padding:0;background:#f3f4f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:36px 16px;"><tr><td align="center">
<table width="100%" style="max-width:520px;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">

  <!-- Header -->
  <tr><td style="background:{hero_color};padding:28px 36px 24px;">
    <p style="margin:0;font-size:13px;color:rgba(255,255,255,.7);letter-spacing:.5px;">ΕΒΔΟΜΑΔΙΑΙΑ ΑΝΑΦΟΡΑ</p>
    <h1 style="margin:6px 0 0;font-size:22px;color:#fff;font-weight:700;">{name}</h1>
    <p style="margin:4px 0 0;font-size:13px;color:rgba(255,255,255,.8);">{week_start} — {week_end}</p>
  </td></tr>

  <!-- Hero stat -->
  <tr><td style="padding:32px 36px 8px;text-align:center;">
    <p style="margin:0;font-size:56px;font-weight:800;color:{hero_color};line-height:1;">{total}</p>
    <p style="margin:4px 0 0;font-size:16px;color:#374151;font-weight:600;">ραντεβού αυτή την εβδομάδα</p>
    <p style="margin:8px 0 0;font-size:14px;">{trend}</p>
  </td></tr>

  <!-- Stats table -->
  <tr><td style="padding:24px 36px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="padding:12px 0;border-bottom:1px solid #f3f4f6;color:#6b7280;font-size:14px;">Νέοι πελάτες</td>
        <td style="padding:12px 0;border-bottom:1px solid #f3f4f6;font-weight:600;font-size:14px;text-align:right;">{new_cl}</td>
      </tr>
      <tr>
        <td style="padding:12px 0;border-bottom:1px solid #f3f4f6;color:#6b7280;font-size:14px;">Ακυρώσεις</td>
        <td style="padding:12px 0;border-bottom:1px solid #f3f4f6;font-weight:600;font-size:14px;text-align:right;">{cancelled}</td>
      </tr>{revenue_html}{top_svc_html}{memory_html}
    </table>
  </td></tr>

  <!-- Message -->
  <tr><td style="padding:0 36px 16px;">
    <p style="margin:0;font-size:14px;color:#6b7280;background:#f9fafb;padding:16px;border-radius:10px;">{summary_msg}</p>
  </td></tr>

  <!-- Compliance pill -->
  <tr><td style="padding:0 36px 28px;">
    <div style="background:linear-gradient(135deg,#F3F1FA,#FAFAFB);border:1px solid #E6E6EA;border-radius:10px;padding:14px 16px;font-size:12px;color:#4B4B53;">
      🛡️ <strong>Compliance status:</strong> EU AI Act + GDPR active ·
      <a href="https://www.ran-tevu.gr/compliance" style="color:#6E56CF;text-decoration:none;">δες τη σελίδα</a>
    </div>
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#f9fafb;padding:20px 36px;border-top:1px solid #f3f4f6;">
    <p style="margin:0;font-size:12px;color:#9ca3af;text-align:center;">
      Αναφορά από <strong>Ran-Tevu</strong> · ran-tevu.gr<br/>
      Αυτό το email αποστέλλεται αυτόματα κάθε Δευτέρα πρωί.
    </p>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""

        _send_via_resend(to=owner_email, subject=subject, html=html)
        print(f"[WEEKLY REPORT ✅] → {name} ({owner_email})")
    except Exception as e:
        print(f"[WEEKLY REPORT ERROR] {e}")


# ════════════════════════════════════════════════════════════════════
# Public wrapper για cross-module use (Growth, Store, Scheduler)
# ════════════════════════════════════════════════════════════════════

def send_email_via_resend(to_email: str, subject: str, html: str,
                          from_name: str = None) -> bool:
    """Public Resend wrapper. Allows custom from_name per-send.
    Returns True αν στάλθηκε, False αλλιώς.

    Used by: Growth Engine, Store delivery, Scheduler approval emails.
    """
    if not RESEND_API_KEY:
        print(f"[RESEND SKIP] no API key — to={to_email}")
        return False

    sender = f"{from_name} <onboarding@resend.dev>" if from_name else _from_addr()
    if from_name and FROM_DOMAIN:
        sender = f"{from_name} <noreply@{FROM_DOMAIN}>"

    payload = json.dumps({
        "from":    sender,
        "to":      [to_email],
        "subject": subject,
        "html":    html,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type":  "application/json",
            # ΧΩΡΙΣ User-Agent το Cloudflare του Resend μπλοκάρει με 1010 → δεν φεύγει ΚΑΝΕΝΑ email
            "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
            "Accept":        "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = r.status in (200, 201)
            if ok:
                print(f"[RESEND ✅] → {to_email}: {subject[:60]}")
            return ok
    except Exception as e:
        try:
            from debug_log import log_error
            log_error(f"send_email_via_resend(to={to_email}, subj={subject[:40]})", e)
        except Exception:
            print(f"[RESEND ERROR] {to_email}: {type(e).__name__}: {e}")
        return False
