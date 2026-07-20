"""
routes/inbox.py — Feature #2: Multi-channel Unified Inbox (2026-05-28)

Status: SKELETON ready, channels wait for credentials.

Architecture:
  - WhatsApp Business Cloud API (Meta) — fastest channel to activate
  - Instagram Graph API (Meta) — same Meta Business account
  - Booking.com Connect API — requires partner agreement (3-6 months)
  - Airbnb API — no longer public; rely on email forwarding

Same AI brain (memory_service + Claude) responds across all channels.

Activation steps (user does manually):
  1. WhatsApp:
     - Create Meta Business Account
     - Sign up for WhatsApp Business Platform via BSP (Twilio/360dialog/Wati)
     - Get Phone Number ID + Access Token
     - Set env: META_WHATSAPP_PHONE_ID, META_WHATSAPP_TOKEN, META_VERIFY_TOKEN
     - Configure webhook in Meta: https://www.ran-tevu.gr/webhooks/whatsapp
  2. Instagram:
     - Connect Instagram Business account to Meta Business
     - Same META_WHATSAPP_TOKEN works
     - Set env: META_INSTAGRAM_USER_ID
     - Webhook: https://www.ran-tevu.gr/webhooks/instagram
  3. Booking.com:
     - Apply to Booking.com Connect Partner Programme (BPP)
     - Wait ~3-6 months for approval
     - Implement OAuth + their messaging API

Current implementation:
  - DB tables: inbox_threads, inbox_messages
  - Webhook receivers for WhatsApp + Instagram (Meta verify + message routing)
  - Unified inbox dashboard endpoint
  - AI auto-reply hook (uses existing memory_service)
"""
from __future__ import annotations

import os
import json
from datetime import datetime
from flask import Blueprint, request, jsonify

import database as db
from admin_auth import admin_ok, token_from_request


bp = Blueprint("inbox", __name__)


# ── DB helpers (will move to database.py in next iteration) ───────────

def _upsert_thread(slug: str, channel: str, external_id: str,
                   guest_identifier: str, guest_name: str = "",
                   guest_language: str = "el") -> int:
    """Get-or-create thread for a (slug, channel, external_id) tuple."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db._conn() as c:
        existing = c.execute(
            "SELECT id FROM inbox_threads WHERE slug=? AND channel=? AND external_id=?",
            (slug, channel, external_id)
        ).fetchone()
        if existing:
            c.execute("UPDATE inbox_threads SET last_message_at=? WHERE id=?",
                      (now, existing["id"]))
            c.commit()
            return existing["id"]
        cur = c.execute("""
            INSERT INTO inbox_threads
                (slug, channel, external_id, guest_identifier, guest_name,
                 guest_language, last_message_at)
            VALUES (?,?,?,?,?,?,?)
        """, (slug, channel, external_id, guest_identifier, guest_name,
              guest_language, now))
        c.commit()
        return cur.lastrowid


def _save_message(thread_id: int, direction: str, sender: str, body: str,
                  media_url: str = "", external_msg_id: str = "",
                  ai_handled: bool = False) -> int:
    with db._conn() as c:
        cur = c.execute("""
            INSERT INTO inbox_messages
                (thread_id, direction, sender, body, media_url,
                 external_msg_id, ai_handled)
            VALUES (?,?,?,?,?,?,?)
        """, (thread_id, direction, sender, body[:5000], media_url,
              external_msg_id, 1 if ai_handled else 0))
        c.commit()
        return cur.lastrowid


# ── Business slug resolution (multi-tenant: many clinics, one Meta webhook) ──

def _slug_for_channel(channel_col: str, external_channel_id: str, env_default_key: str) -> str:
    """Which business owns this WhatsApp phone_number_id / Instagram page id? A business
    connects a channel by setting businesses.whatsapp_phone_id / instagram_page_id (e.g. from
    the onboarding/settings UI). No business has claimed it yet (or none was sent) → fall back
    to the single-tenant env default, which is today's behavior."""
    if external_channel_id:
        try:
            with db._conn() as c:
                row = c.execute(
                    f"SELECT slug FROM businesses WHERE {channel_col}=? LIMIT 1",
                    (external_channel_id,)
                ).fetchone()
                if row:
                    return row["slug"]
        except Exception as e:
            print(f"[INBOX] slug lookup failed ({channel_col}={external_channel_id}): {e}")
    return os.environ.get(env_default_key, "default")


# ── WhatsApp Cloud API (Meta) ──────────────────────────────────────────

@bp.get("/webhooks/whatsapp")
def whatsapp_verify():
    """Meta verification handshake."""
    verify_token = os.environ.get("META_VERIFY_TOKEN", "ran-tevu-meta-verify")
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == verify_token:
        return challenge, 200
    return "verification_failed", 403


@bp.post("/webhooks/whatsapp")
def whatsapp_message():
    """Receives WhatsApp messages and routes to AI brain."""
    payload = request.get_json(silent=True) or {}
    try:
        entry = (payload.get("entry") or [{}])[0]
        changes = (entry.get("changes") or [{}])[0]
        value = changes.get("value", {}) or {}
        messages = value.get("messages") or []
        contacts = value.get("contacts") or []
        phone_number_id = (value.get("metadata") or {}).get("phone_number_id", "")

        for msg in messages:
            from_number = msg.get("from", "")
            text = (msg.get("text") or {}).get("body", "")
            msg_id = msg.get("id", "")

            contact_name = ""
            if contacts:
                contact_name = (contacts[0].get("profile") or {}).get("name", "")

            slug = _slug_for_channel("whatsapp_phone_id", phone_number_id, "DEFAULT_WHATSAPP_SLUG")

            thread_id = _upsert_thread(
                slug=slug,
                channel="whatsapp",
                external_id=from_number,  # use sender phone as thread id
                guest_identifier=from_number,
                guest_name=contact_name,
            )
            _save_message(thread_id, "inbound", "guest", text,
                          external_msg_id=msg_id)

            _trigger_ai_reply(slug, thread_id, from_number, text, channel="whatsapp",
                              channel_meta={"phone_number_id": phone_number_id})

    except Exception as e:
        print(f"[INBOX/whatsapp] parse error: {e}")
        return jsonify({"received": True, "error": str(e)}), 200  # return 200 to Meta anyway

    return jsonify({"received": True})


# ── Instagram (same Meta webhook infrastructure) ──────────────────────

@bp.get("/webhooks/instagram")
def instagram_verify():
    return whatsapp_verify()  # same verify logic


@bp.post("/webhooks/instagram")
def instagram_message():
    """Receives Instagram DMs."""
    payload = request.get_json(silent=True) or {}
    try:
        entry = (payload.get("entry") or [{}])[0]
        messaging = entry.get("messaging") or []
        page_id = entry.get("id", "")  # the IG/page account that received the DM
        for m in messaging:
            sender_id = (m.get("sender") or {}).get("id", "")
            text = (m.get("message") or {}).get("text", "")
            msg_id = (m.get("message") or {}).get("mid", "")
            if not text:
                continue
            slug = _slug_for_channel("instagram_page_id", page_id, "DEFAULT_INSTAGRAM_SLUG")
            thread_id = _upsert_thread(
                slug=slug, channel="instagram",
                external_id=sender_id, guest_identifier=sender_id,
            )
            _save_message(thread_id, "inbound", "guest", text,
                          external_msg_id=msg_id)
            _trigger_ai_reply(slug, thread_id, sender_id, text, channel="instagram",
                              channel_meta={"page_id": page_id})
    except Exception as e:
        print(f"[INBOX/instagram] parse error: {e}")
    return jsonify({"received": True})


# ── Outbound channel adapters (Meta Graph API) ─────────────────────────

def _send_whatsapp(to_number: str, text: str, phone_number_id: str = "") -> bool:
    """POST μέσω WhatsApp Cloud API. False (no crash) αν λείπουν credentials."""
    token = os.environ.get("META_WHATSAPP_TOKEN", "")
    pid = phone_number_id or os.environ.get("META_WHATSAPP_PHONE_ID", "")
    if not (token and pid and to_number):
        print(f"[INBOX/whatsapp] send skipped — missing token/phone_id/recipient")
        return False
    import urllib.request
    body = json.dumps({
        "messaging_product": "whatsapp", "to": to_number,
        "type": "text", "text": {"body": text[:4096]},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://graph.facebook.com/v19.0/{pid}/messages", data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"[INBOX/whatsapp] send failed: {e}")
        return False


def _send_instagram(recipient_id: str, text: str, page_id: str = "") -> bool:
    """POST μέσω Instagram Messaging API (ίδιο Meta Business token με το WhatsApp)."""
    token = os.environ.get("META_WHATSAPP_TOKEN", "")
    pid = page_id or os.environ.get("META_INSTAGRAM_USER_ID", "")
    if not (token and pid and recipient_id):
        print(f"[INBOX/instagram] send skipped — missing token/page_id/recipient")
        return False
    import urllib.request
    body = json.dumps({
        "recipient": {"id": recipient_id}, "message": {"text": text[:1000]},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://graph.facebook.com/v19.0/{pid}/messages", data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"[INBOX/instagram] send failed: {e}")
        return False


# ── AI brain integration ──────────────────────────────────────────────

def _thread_messages(thread_id: int, limit: int = 20) -> list:
    """Thread history → Claude message format (role/content), oldest first."""
    with db._conn() as c:
        rows = c.execute(
            "SELECT direction, sender, body FROM inbox_messages "
            "WHERE thread_id=? ORDER BY created DESC LIMIT ?",
            (thread_id, limit)
        ).fetchall()
    out = []
    for r in reversed(rows):
        role = "user" if r["direction"] == "inbound" else "assistant"
        if r["body"]:
            out.append({"role": role, "content": r["body"][:2000]})
    return out


def _trigger_ai_reply(slug: str, thread_id: int, guest_id: str, text: str,
                      channel: str, channel_meta: dict | None = None):
    """Ίδιος εγκέφαλος με το web chat (/api/<slug>/chat): assemble thread history →
    app.run_chat_turn → στείλε πίσω μέσω του adapter του κάθε καναλιού. Lazy import από
    το app (όχι top-level) γιατί το app.py είναι αυτό που φορτώνει ΑΥΤΟ το blueprint —
    circular αν γινόταν top-level."""
    channel_meta = channel_meta or {}
    try:
        from app import get_business, run_chat_turn
    except Exception as e:
        print(f"[INBOX/AI] δεν φόρτωσε το chat brain: {e}")
        return

    business = get_business(slug)
    if not business:
        print(f"[INBOX/AI] καμία επιχείρηση για slug={slug} — αγνοείται (webhook not yet mapped)")
        return

    lang = business.get("lang") if business.get("lang") in ("el", "en") else "el"
    messages = _thread_messages(thread_id)
    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": text[:2000]})

    try:
        reply = run_chat_turn(slug, business, messages, lang)
    except Exception as e:
        print(f"[INBOX/AI] chat brain error ({channel}:{guest_id}): {e}")
        return

    if channel == "whatsapp":
        sent = _send_whatsapp(guest_id, reply, channel_meta.get("phone_number_id", ""))
    elif channel == "instagram":
        sent = _send_instagram(guest_id, reply, channel_meta.get("page_id", ""))
    else:
        sent = False

    _save_message(thread_id, "outbound", "ai", reply, ai_handled=True)
    if not sent:
        print(f"[INBOX/AI] απάντηση παράχθηκε αλλά ΔΕΝ στάλθηκε ({channel}:{guest_id}) — "
              f"λείπουν Meta credentials (META_WHATSAPP_TOKEN + PHONE_ID/INSTAGRAM_USER_ID). "
              f"Καταγράφηκε στο thread ώστε ο γιατρός να τη δει/στείλει χειροκίνητα.")


# ── Admin endpoints (unified inbox dashboard) ─────────────────────────

def _require_admin():
    """Constant-time, fail-closed (shared admin_auth — same check as app.py's require_master)."""
    if not admin_ok(token_from_request()):
        return jsonify({"error": "unauthorized"}), 401
    return None


@bp.get("/api/admin/<slug>/inbox/threads")
def admin_inbox_threads(slug):
    err = _require_admin()
    if err: return err
    status = request.args.get("status", "open")
    with db._conn() as c:
        rows = c.execute("""
            SELECT t.*,
                   (SELECT body FROM inbox_messages m
                    WHERE m.thread_id=t.id ORDER BY m.created DESC LIMIT 1) AS last_message
            FROM inbox_threads t
            WHERE t.slug=? AND t.status=?
            ORDER BY t.last_message_at DESC
            LIMIT 100
        """, (slug, status)).fetchall()
    return jsonify({"threads": [dict(r) for r in rows]})


@bp.get("/api/admin/<slug>/inbox/threads/<int:thread_id>/messages")
def admin_inbox_messages(slug, thread_id):
    err = _require_admin()
    if err: return err
    with db._conn() as c:
        # verify the thread belongs to this slug
        t = c.execute("SELECT slug FROM inbox_threads WHERE id=?", (thread_id,)).fetchone()
        if not t or t["slug"] != slug:
            return jsonify({"error": "not_found"}), 404
        rows = c.execute("""
            SELECT * FROM inbox_messages
            WHERE thread_id=?
            ORDER BY created ASC
            LIMIT 500
        """, (thread_id,)).fetchall()
    return jsonify({"messages": [dict(r) for r in rows]})
