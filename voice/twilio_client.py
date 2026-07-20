"""
Twilio REST API wrapper.

Δύο διαδρομές:
1. Buy a Greek number programmatically (~$1/month + per-min)
2. Reuse an existing Twilio number you own

Docs: https://www.twilio.com/docs/iam/keys/api-key
"""
import os
import requests
from typing import Optional
from requests.auth import HTTPBasicAuth

TWILIO_BASE = "https://api.twilio.com/2010-04-01"


class TwilioError(Exception):
    pass


def _creds() -> tuple[str, str]:
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        raise TwilioError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN required")
    return sid, token


def _request(method: str, path: str, data: Optional[dict] = None,
             timeout: int = 20) -> dict:
    sid, token = _creds()
    url = f"{TWILIO_BASE}/Accounts/{sid}{path}"
    try:
        r = requests.request(method, url, data=data,
                             auth=HTTPBasicAuth(sid, token), timeout=timeout)
    except requests.RequestException as e:
        raise TwilioError(f"Network error: {e}")
    if r.status_code >= 400:
        raise TwilioError(f"Twilio {method} {path} → {r.status_code}: {r.text[:500]}")
    if r.text:
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}
    return {}


# ── Number search & purchase ────────────────────────────────────────────

def search_available_greek_numbers(area_code: str = "21",
                                   limit: int = 10) -> list:
    """Search available Greek phone numbers (default Athens 21X).
    area_code: '21' for Athens, '281' for Heraklion, '2831' for Rethymno."""
    sid, _ = _creds()
    url = f"{TWILIO_BASE}/Accounts/{sid}/AvailablePhoneNumbers/GR/Local.json"
    params = {"AreaCode": area_code, "Limit": limit, "VoiceEnabled": "true"}
    r = requests.get(url, params=params, auth=HTTPBasicAuth(*_creds()),
                     timeout=15)
    if r.status_code >= 400:
        raise TwilioError(f"Twilio number search failed: {r.text[:500]}")
    return r.json().get("available_phone_numbers", [])


def purchase_number(phone_number: str, voice_webhook_url: Optional[str] = None,
                    friendly_name: str = "") -> dict:
    """Buy a phone number. Returns Twilio number object with SID.
    Note: Greek numbers may require address verification on Twilio account."""
    data = {
        "PhoneNumber": phone_number,
        "FriendlyName": friendly_name or phone_number,
    }
    if voice_webhook_url:
        # If you bypass Vapi and want raw Twilio voice control:
        data["VoiceUrl"] = voice_webhook_url
        data["VoiceMethod"] = "POST"
    return _request("POST", "/IncomingPhoneNumbers.json", data=data)


def release_number(phone_number_sid: str) -> dict:
    """Release (stop paying for) a phone number."""
    return _request("DELETE", f"/IncomingPhoneNumbers/{phone_number_sid}.json")


def list_owned_numbers() -> list:
    resp = _request("GET", "/IncomingPhoneNumbers.json")
    return resp.get("incoming_phone_numbers", [])


# ── SMS (για appointment confirmations) ────────────────────────────────

def send_sms(to_number: str, body: str,
            from_number: Optional[str] = None) -> dict:
    """Send SMS via Twilio. from_number must be a Twilio-owned number."""
    # δέξου όποιο env name κι αν οριστεί (ευθυγράμμιση με sms_service.py)
    from_num = (from_number or os.environ.get("TWILIO_DEFAULT_FROM")
                or os.environ.get("TWILIO_PHONE_NUMBER") or os.environ.get("TWILIO_FROM_NUMBER"))
    if not from_num:
        raise TwilioError("from_number or TWILIO_DEFAULT_FROM required")
    return _request("POST", "/Messages.json", data={
        "To": to_number,
        "From": from_num,
        "Body": body[:1600],  # SMS hard limit
    })


# ── Helpers ────────────────────────────────────────────────────────────

def is_configured() -> bool:
    # ΚΑΙ from-number — αλλιώς is_configured()→True αλλά send_sms σκάει σιωπηλά
    # (η AI έλεγε «θα λάβετε SMS» χωρίς να φεύγει τίποτα)
    return bool(os.environ.get("TWILIO_ACCOUNT_SID")
                and os.environ.get("TWILIO_AUTH_TOKEN")
                and (os.environ.get("TWILIO_DEFAULT_FROM")
                     or os.environ.get("TWILIO_PHONE_NUMBER")
                     or os.environ.get("TWILIO_FROM_NUMBER")))


def healthcheck() -> dict:
    if not is_configured():
        return {"ok": False, "configured": False, "error": "credentials missing"}
    try:
        sid, _ = _creds()
        return {"ok": True, "configured": True, "account_sid": sid[:10] + "..."}
    except TwilioError as e:
        return {"ok": False, "configured": True, "error": str(e)}
