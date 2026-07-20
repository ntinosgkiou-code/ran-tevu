"""
channels.py — outbound message channels: REAL providers when keys exist, durable outbox otherwise.
==================================================================================================
The last mile of autonomy. When the clinic plugs its own provider keys (env vars), an approved
outreach actually sends; without keys, every message lands in a DURABLE outbox queue (SQLite via
the clinic store) with an explicit reason — the system never pretends it sent something.

  SMS   → Twilio  (TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN + TWILIO_PHONE_NUMBER)
  Email → Resend  (RESEND_API_KEY + FROM_DOMAIN)

Real sends go through `tenacity` exponential backoff (3 attempts) and are ALWAYS audited
(what/why/who/provider-id). Live-send code paths are exercised only when keys are configured —
the test suite runs key-less and proves the queue semantics. GDPR: callers must pass consent-
checked recipients; this layer additionally refuses to send to an empty contact.

Tests: python core/test_channels.py
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from audit import AUDIT
from clinic_store import ClinicStore


class MessageChannels:
    """Dispatcher over SMS/email. Construct once with the clinic store (for the durable outbox)."""

    def __init__(self, store: ClinicStore) -> None:
        self.store = store
        self.sms_ready = all(os.environ.get(k) for k in
                             ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"))
        self.email_ready = all(os.environ.get(k) for k in ("RESEND_API_KEY", "FROM_DOMAIN"))

    # ---- provider calls (only reachable when the clinic configured its keys) ----
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=4), reraise=True)
    def _twilio_send(self, to: str, text: str) -> str:
        sid = os.environ["TWILIO_ACCOUNT_SID"]
        r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                       auth=(sid, os.environ["TWILIO_AUTH_TOKEN"]),
                       data={"From": os.environ["TWILIO_PHONE_NUMBER"], "To": to, "Body": text},
                       timeout=15.0)
        r.raise_for_status()
        return r.json().get("sid", "sent")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=4), reraise=True)
    def _resend_send(self, to: str, subject: str, text: str) -> str:
        r = httpx.post("https://api.resend.com/emails",
                       headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
                       json={"from": f"clinic@{os.environ['FROM_DOMAIN']}",
                             "to": [to], "subject": subject, "text": text},
                       timeout=15.0)
        r.raise_for_status()
        return r.json().get("id", "sent")

    # ---- the one public entrypoint ----
    def dispatch(self, kind: str, to: str, text: str, why: str = "",
                 approved_by: str = "") -> dict:
        """Send (kind: sms|email) if the provider is configured; otherwise queue durably.
        Returns an honest status — 'sent' ONLY when a provider actually accepted it."""
        mid = f"msg-{uuid.uuid4().hex[:8]}"
        if not to.strip():
            rec = {"id": mid, "kind": kind, "to": "", "text": text, "status": "blocked",
                   "reason": "no contact channel on record", "why": why}
            self.store.kv.put(self.store._ns("outbox"), mid, rec)
            AUDIT.record("channel.blocked", {"id": mid, "kind": kind, "reason": rec["reason"]})
            return rec

        ready = self.sms_ready if kind == "sms" else self.email_ready
        if ready:
            try:
                provider_id = (self._twilio_send(to, text) if kind == "sms"
                               else self._resend_send(to, "Ενημέρωση από το ιατρείο σας", text))
                rec = {"id": mid, "kind": kind, "to": to, "text": text, "status": "sent",
                       "provider_id": provider_id, "why": why, "approved_by": approved_by}
                self.store.kv.put(self.store._ns("outbox"), mid, rec)
                AUDIT.record("channel.sent", {"id": mid, "kind": kind,
                                              "provider_id": provider_id, "by": approved_by})
                return rec
            except Exception as e:                      # provider down → queue, never lose it
                rec = {"id": mid, "kind": kind, "to": to, "text": text, "status": "queued",
                       "reason": f"provider error after retries: {type(e).__name__}", "why": why}
                self.store.kv.put(self.store._ns("outbox"), mid, rec)
                AUDIT.record("channel.queued", {"id": mid, "kind": kind, "reason": rec["reason"]})
                return rec

        rec = {"id": mid, "kind": kind, "to": to, "text": text, "status": "queued",
               "reason": ("no provider keys — set TWILIO_* to go live" if kind == "sms"
                          else "no provider keys — set RESEND_API_KEY + FROM_DOMAIN to go live"),
               "why": why, "approved_by": approved_by}
        self.store.kv.put(self.store._ns("outbox"), mid, rec)
        AUDIT.record("channel.queued", {"id": mid, "kind": kind, "reason": "no_keys"})
        return rec

    def outbox(self) -> list[dict]:
        return list(self.store.kv.all(self.store._ns("outbox")).values())

    def status(self) -> dict:
        queued = [m for m in self.outbox() if m["status"] == "queued"]
        return {"sms_live": self.sms_ready, "email_live": self.email_ready,
                "queued": len(queued), "sent": sum(1 for m in self.outbox() if m["status"] == "sent"),
                "mode": ("LIVE" if (self.sms_ready or self.email_ready) else
                         "OUTBOX (βάλε provider keys για αποστολή)")}


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cs = ClinicStore(":memory:")
    ch = MessageChannels(cs)
    print("status:", ch.status())
    r = ch.dispatch("sms", "+306945123456", "Υπενθύμιση ραντεβού αύριο 17:00", why="reminder")
    print("dispatch (no keys):", r["status"], "—", r.get("reason"))
    print("outbox:", len(ch.outbox()))
