"""
Vapi.ai REST API wrapper.

Docs: https://docs.vapi.ai/api-reference

Έχει 3 βασικές οντότητες:
- Assistant   — η AI personality (LLM + voice + prompts + tools)
- Phone Number — το νούμερο που δένει με Twilio + assistant
- Call        — μια κλήση (logged automatically)
"""
import os
import json
import requests
from typing import Optional

VAPI_BASE = "https://api.vapi.ai"


class VapiError(Exception):
    pass


def _headers() -> dict:
    key = os.environ.get("VAPI_API_KEY")
    if not key:
        raise VapiError("VAPI_API_KEY not configured in environment")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, json_body: Optional[dict] = None,
             timeout: int = 20) -> dict:
    url = f"{VAPI_BASE}{path}"
    try:
        r = requests.request(method, url, headers=_headers(),
                             json=json_body, timeout=timeout)
    except requests.RequestException as e:
        raise VapiError(f"Network error: {e}")
    if r.status_code >= 400:
        raise VapiError(f"Vapi {method} {path} → {r.status_code}: {r.text[:500]}")
    if r.text:
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}
    return {}


# ── Assistants ──────────────────────────────────────────────────────────

def create_assistant(config: dict) -> dict:
    """Create a new Vapi assistant. Returns full assistant object with id."""
    return _request("POST", "/assistant", json_body=config)


def update_assistant(assistant_id: str, patch: dict) -> dict:
    """PATCH partial fields on an existing assistant."""
    return _request("PATCH", f"/assistant/{assistant_id}", json_body=patch)


def get_assistant(assistant_id: str) -> dict:
    return _request("GET", f"/assistant/{assistant_id}")


def delete_assistant(assistant_id: str) -> dict:
    return _request("DELETE", f"/assistant/{assistant_id}")


def list_assistants(limit: int = 100) -> list:
    resp = _request("GET", f"/assistant?limit={limit}")
    return resp if isinstance(resp, list) else resp.get("data", [])


# ── Phone Numbers (link Twilio number to assistant) ────────────────────

def attach_twilio_number(twilio_sid: str, twilio_token: str,
                        twilio_phone: str, assistant_id: str,
                        name: str = "") -> dict:
    """Register a Twilio phone number with Vapi, bound to an assistant.
    Vapi will receive all calls and respond via the assistant."""
    return _request("POST", "/phone-number", json_body={
        "provider": "twilio",
        "number": twilio_phone,
        "twilioAccountSid": twilio_sid,
        "twilioAuthToken": twilio_token,
        "assistantId": assistant_id,
        "name": name or twilio_phone,
    })


def create_vapi_number(assistant_id: str, name: str = "") -> dict:
    """Provision ΔΩΡΕΑΝ Vapi-managed νούμερο (US), δεμένο σε assistant — ΧΩΡΙΣ Twilio account.
    Αυτό κάνει το voice turnkey (1-click) με μόνο το VAPI_API_KEY.
    (US number· production model = ο γιατρός κάνει call-forwarding του δικού του νούμερου εδώ.
     Για ελληνικό DID → attach_twilio_number ή SIP.)"""
    return _request("POST", "/phone-number", json_body={
        "provider": "vapi",
        "assistantId": assistant_id,
        "name": name or "ran-tevu-line",
    })


def create_sip_credential(name: str, gateway_ip: str, username: str,
                          password: str, inbound: bool = True) -> dict:
    """Δημιουργεί BYO SIP-trunk credential ΜΙΑ ΦΟΡΑ (account-level), για να συνδέεις
    ΕΛΛΗΝΙΚΑ DID (Zadarma/Telnyx/DIDWW κλπ). Επιστρέφει credential με id →
    βάλ' το στο Railway ως VAPI_SIP_CREDENTIAL_ID και μετά κάθε πελάτης παίρνει GR αριθμό."""
    return _request("POST", "/credential", json_body={
        "provider": "byo-sip-trunk",
        "name": name or "ran-tevu-sip",
        "gateways": [{"ip": gateway_ip, "inboundEnabled": bool(inbound)}],
        "outboundLeadingPlusEnabled": True,
        "outboundAuthenticationPlan": {
            "authUsername": username,
            "authPassword": password,
        },
    })


def attach_sip_number(number: str, credential_id: str, assistant_id: str,
                      name: str = "") -> dict:
    """Συνδέει ΕΛΛΗΝΙΚΟ DID (+30…) μέσω BYO SIP με assistant. Έτσι ΔΕΝ χρεώνεται
    διεθνώς ούτε ο γιατρός (προώθηση) ούτε ο ασθενής (κλήση) — όλα ελληνικά/αστικά."""
    return _request("POST", "/phone-number", json_body={
        "provider": "byo-phone-number",
        "name": name or f"gr-{number}",
        "number": number,
        "numberE164CheckEnabled": False,
        "credentialId": credential_id,
        "assistantId": assistant_id,
    })


def update_phone_assistant(phone_number_id: str, assistant_id: str) -> dict:
    """Re-route an existing Vapi phone number to a different assistant."""
    return _request("PATCH", f"/phone-number/{phone_number_id}", json_body={
        "assistantId": assistant_id,
    })


def delete_phone_number(phone_number_id: str) -> dict:
    return _request("DELETE", f"/phone-number/{phone_number_id}")


# ── Calls ──────────────────────────────────────────────────────────────

def get_call(call_id: str) -> dict:
    return _request("GET", f"/call/{call_id}")


def list_calls(assistant_id: Optional[str] = None, limit: int = 50) -> list:
    path = f"/call?limit={limit}"
    if assistant_id:
        path += f"&assistantId={assistant_id}"
    resp = _request("GET", path)
    return resp if isinstance(resp, list) else resp.get("data", [])


def create_outbound_call(phone_number_id: str, assistant_id: str,
                         to_number: str, customer_name: str = "",
                         first_message: str = "") -> dict:
    """Trigger outbound call (for reminders, follow-ups, no-show recovery).
    first_message: αν δοθεί, ο assistant ΞΕΚΙΝΑΕΙ λέγοντας αυτό (π.χ. η ημερήσια
    σύνοψη στον γιατρό) μέσω assistantOverrides.firstMessage."""
    body = {
        "phoneNumberId": phone_number_id,
        "assistantId": assistant_id,
        "customer": {
            "number": to_number,
            "name": customer_name,
        },
    }
    if first_message:
        body["assistantOverrides"] = {"firstMessage": first_message[:900]}
    return _request("POST", "/call", json_body=body)


# ── Helpers ────────────────────────────────────────────────────────────

def is_configured() -> bool:
    """True if VAPI_API_KEY is set."""
    return bool(os.environ.get("VAPI_API_KEY"))


def healthcheck() -> dict:
    """Lightweight: list 1 assistant to verify API key works."""
    try:
        _request("GET", "/assistant?limit=1")
        return {"ok": True, "configured": True}
    except VapiError as e:
        return {"ok": False, "configured": is_configured(), "error": str(e)}
