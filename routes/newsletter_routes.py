"""
newsletter_routes.py — «Ιατρική Νοημοσύνη»: εγγραφές, απεγγραφή, αποστολή τεύχους.
==================================================================================
Opt-in ΜΟΝΟ (GDPR): γράφεται όποιος το ζητήσει από τη φόρμα — ποτέ import cold λίστας.

  POST /api/newsletter/subscribe            {email}            → εγγραφή (idempotent)
  GET  /newsletter/unsubscribe/<token>                          → απεγγραφή με 1 κλικ
  GET  /api/admin/newsletter/subscribers    (ADMIN_TOKEN)       → count + λίστα
  POST /api/admin/newsletter/send           (ADMIN_TOKEN)       → στέλνει τεύχος σε όλους
        body: {subject, html, test_email?}  — με test_email στέλνει ΜΟΝΟ εκεί (δοκιμή πριν το blast)

Κάθε αποστολή βάζει αυτόματα προσωποποιημένο unsubscribe link στο τέλος ({unsubscribe_url}
placeholder στο html, αλλιώς append footer). Αποστολή μέσω Resend (email_service).
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
from functools import wraps

from flask import Blueprint, jsonify, request

import database as db

bp = Blueprint("newsletter", __name__)

_EMAIL_RX = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _init_schema() -> None:
    with db._conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS newsletter_subscribers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT UNIQUE NOT NULL,
                token      TEXT UNIQUE NOT NULL,
                status     TEXT NOT NULL DEFAULT 'active',
                created    TEXT NOT NULL DEFAULT (datetime('now')),
                unsubbed   TEXT
            )
        """)
        c.commit()


try:
    _init_schema()
except Exception as _e:
    print(f"[NEWSLETTER] schema init skipped: {_e}")


def require_master(fn):
    @wraps(fn)
    def wrap(*a, **k):
        token = (request.headers.get("Authorization", "").replace("Bearer ", "").strip()
                 or request.args.get("token", "").strip())
        admin = os.environ.get("ADMIN_TOKEN", "")
        if not admin or not token or not hmac.compare_digest(token, admin):  # fail-closed
            return jsonify({"error": "unauthorized"}), 401
        return fn(*a, **k)
    return wrap


def _base_url() -> str:
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("BASE_URL", "")
    if domain:
        return f"https://{domain}" if not domain.startswith("http") else domain
    return "https://www.ran-tevu.gr"


@bp.post("/api/newsletter/subscribe")
def subscribe():
    d = request.get_json(silent=True) or request.form
    email = (d.get("email") or "").strip().lower()
    if not _EMAIL_RX.match(email):
        return jsonify({"ok": False, "msg": "Δώσε ένα έγκυρο email."}), 400
    with db._conn() as c:
        row = c.execute("SELECT id, status FROM newsletter_subscribers WHERE email=?",
                        (email,)).fetchone()
        if row:
            if row["status"] != "active":   # ξαναγράφτηκε μετά από απεγγραφή → reactivate
                c.execute("UPDATE newsletter_subscribers SET status='active', unsubbed=NULL "
                          "WHERE id=?", (row["id"],))
                c.commit()
            return jsonify({"ok": True})    # idempotent — δεν αποκαλύπτουμε αν υπήρχε ήδη
        c.execute("INSERT INTO newsletter_subscribers (email, token) VALUES (?, ?)",
                  (email, secrets.token_urlsafe(24)))
        c.commit()
    return jsonify({"ok": True})


@bp.get("/newsletter/unsubscribe/<token>")
def unsubscribe(token: str):
    with db._conn() as c:
        row = c.execute("SELECT id FROM newsletter_subscribers WHERE token=? AND status='active'",
                        (token,)).fetchone()
        if row:
            c.execute("UPDATE newsletter_subscribers SET status='unsubscribed', "
                      "unsubbed=datetime('now') WHERE id=?", (row["id"],))
            c.commit()
    # Ίδια απάντηση είτε βρέθηκε είτε όχι (no token probing) — και πάντα ευγενική.
    return ("""<!doctype html><html lang="el"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Απεγγραφή</title></head>
<body style="font-family:system-ui,sans-serif;background:#fafaf9;color:#1c1917;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0">
<div style="text-align:center;padding:40px;max-width:420px">
<div style="font-size:40px;margin-bottom:14px">👋</div>
<h1 style="font-size:20px;margin:0 0 8px">Έγινε — απεγγράφηκες.</h1>
<p style="color:#57534e;font-size:14.5px;line-height:1.6">Δεν θα ξαναλάβεις email από το
newsletter. Αν αλλάξεις γνώμη, μπορείς να ξαναγραφτείς όποτε θες από το site.</p>
</div></body></html>""", 200, {"Content-Type": "text/html; charset=utf-8"})


@bp.get("/api/admin/newsletter/subscribers")
@require_master
def list_subscribers():
    with db._conn() as c:
        rows = c.execute("SELECT email, status, created FROM newsletter_subscribers "
                         "ORDER BY id DESC").fetchall()
    active = [r["email"] for r in rows if r["status"] == "active"]
    return jsonify({"total": len(rows), "active": len(active),
                    "subscribers": [dict(r) for r in rows[:500]]})


@bp.post("/api/admin/newsletter/send")
@require_master
def send_issue():
    """Στέλνει ένα τεύχος. ΠΑΝΤΑ δοκίμασε πρώτα με test_email πριν το στείλεις σε όλους."""
    d = request.get_json(silent=True) or {}
    subject = (d.get("subject") or "").strip()
    html = (d.get("html") or "").strip()
    test_email = (d.get("test_email") or "").strip().lower()
    if not subject or not html:
        return jsonify({"ok": False, "msg": "Χρειάζονται subject και html."}), 400

    from email_service import _send_via_resend

    def _payload(token: str) -> str:
        url = f"{_base_url()}/newsletter/unsubscribe/{token}"
        if "{unsubscribe_url}" in html:
            return html.replace("{unsubscribe_url}", url)
        return (html + f'<p style="font-size:12px;color:#a8a29e;margin-top:28px">'
                       f'Λαμβάνεις αυτό το email επειδή γράφτηκες στο newsletter. '
                       f'<a href="{url}" style="color:#a8a29e">Απεγγραφή με ένα κλικ</a>.</p>')

    if test_email:   # δοκιμαστική αποστολή ΜΟΝΟ στον αποστολέα — τίποτα προς τη λίστα
        ok = _send_via_resend(test_email, f"[TEST] {subject}", _payload("test-token"))
        return jsonify({"ok": bool(ok), "mode": "test", "to": test_email})

    with db._conn() as c:
        subs = c.execute("SELECT email, token FROM newsletter_subscribers "
                         "WHERE status='active' ORDER BY id").fetchall()
    sent = failed = 0
    for s in subs:
        try:
            if _send_via_resend(s["email"], subject, _payload(s["token"])):
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return jsonify({"ok": True, "mode": "broadcast", "sent": sent, "failed": failed,
                    "total_active": len(subs)})


def register(app):
    app.register_blueprint(bp)
    print("[NEWSLETTER] blueprint loaded — /api/newsletter/subscribe · admin send")
