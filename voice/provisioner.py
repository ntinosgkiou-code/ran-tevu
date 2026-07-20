"""
End-to-end voice agent provisioning.

Παίρνει ένα business και:
1. Δημιουργεί ή updates Vapi assistant
2. Αγοράζει ή reuse Twilio Greek number
3. Συνδέει το νούμερο με τον assistant
4. Σώζει τα IDs στη DB (voice_agents table)
5. Επιστρέφει το νούμερο που θα δώσεις στον πελάτη

Idempotent: αν υπάρχει ήδη voice agent για το slug, κάνει update.
"""
import os
import json
from typing import Optional
from voice import vapi_client, twilio_client
from voice.assistant_builder import build_assistant_config
from voice.elevenlabs_client import pick_voice_for_business
import database as db


class ProvisionError(Exception):
    pass


def _get_existing_agent(slug: str) -> Optional[dict]:
    with db._conn() as c:
        row = c.execute(
            "SELECT * FROM voice_agents WHERE business_slug=?", (slug,)
        ).fetchone()
        return dict(row) if row else None


def _save_agent(slug: str, data: dict) -> int:
    """Insert or update voice_agents row."""
    existing = _get_existing_agent(slug)
    with db._conn() as c:
        if existing:
            c.execute("""
                UPDATE voice_agents SET
                    vapi_assistant_id=?, vapi_phone_id=?, twilio_phone_sid=?,
                    twilio_phone_number=?, voice_key=?, assistant_name=?,
                    status=?, last_provisioned=datetime('now'),
                    config_json=?
                WHERE business_slug=?
            """, (
                data.get("vapi_assistant_id"), data.get("vapi_phone_id"),
                data.get("twilio_phone_sid"), data.get("twilio_phone_number"),
                data.get("voice_key"), data.get("assistant_name"),
                data.get("status", "active"),
                json.dumps(data.get("config", {}), ensure_ascii=False),
                slug,
            ))
            c.commit()
            return existing["id"]
        cur = c.execute("""
            INSERT INTO voice_agents
                (business_slug, vapi_assistant_id, vapi_phone_id,
                 twilio_phone_sid, twilio_phone_number, voice_key,
                 assistant_name, status, last_provisioned, config_json)
            VALUES (?,?,?,?,?,?,?,?, datetime('now'), ?)
        """, (
            slug, data.get("vapi_assistant_id"), data.get("vapi_phone_id"),
            data.get("twilio_phone_sid"), data.get("twilio_phone_number"),
            data.get("voice_key"), data.get("assistant_name"),
            data.get("status", "active"),
            json.dumps(data.get("config", {}), ensure_ascii=False),
        ))
        c.commit()
        return cur.lastrowid


def provision_voice_agent(
    business_slug: str,
    voice_key: Optional[str] = None,
    assistant_name: str = "Madlen",
    twilio_phone: Optional[str] = None,
    purchase_new_number: bool = False,
    area_code: str = "2831",   # Rethymno default
    sip_number: Optional[str] = None,         # ΕΛΛΗΝΙΚΟ DID (+30…) μέσω BYO SIP — production
    sip_credential_id: Optional[str] = None,  # default από env VAPI_SIP_CREDENTIAL_ID
    dry_run: bool = False,
) -> dict:
    """Provision ή update voice agent για ένα business.

    Args:
        business_slug: το slug του business (π.χ. 'papadopoulos-clinic')
        voice_key: 'madlen' / 'stefanos' / 'athina' / 'dimitris' (auto αν None)
        assistant_name: το όνομα που λέει η AI για τον εαυτό της
        twilio_phone: αν δοθεί, χρησιμοποιεί υπάρχον Twilio νούμερο (E.164 format)
        purchase_new_number: αν True ΚΑΙ δεν δόθηκε twilio_phone, αγοράζει νέο
        area_code: για purchase (default 2831 Ρέθυμνο)
        dry_run: αν True, δεν κάνει API calls — μόνο επιστρέφει το config

    Returns:
        dict με keys: vapi_assistant_id, twilio_phone_number, status, message
    """
    # 1. Πάρε το business από DB
    business = db.get_business(business_slug)
    if not business:
        raise ProvisionError(f"Business '{business_slug}' not found in DB")

    # 2. Build the Vapi assistant config
    voice_key = voice_key or pick_voice_for_business(business.get("type", ""))
    config = build_assistant_config(
        business, voice_key=voice_key, assistant_name=assistant_name
    )

    if dry_run:
        return {
            "dry_run": True,
            "config_preview": config,
            "voice_key": voice_key,
            "assistant_name": assistant_name,
            "note": "DRY RUN — no API calls made",
        }

    # 3. Check Vapi configured
    if not vapi_client.is_configured():
        raise ProvisionError(
            "VAPI_API_KEY not set. Sign up at vapi.ai → Dashboard → API Keys"
        )

    # 4. Create or update assistant
    existing = _get_existing_agent(business_slug)
    if existing and existing.get("vapi_assistant_id"):
        print(f"[provision] Updating existing assistant {existing['vapi_assistant_id']}")
        assistant = vapi_client.update_assistant(
            existing["vapi_assistant_id"], config
        )
        vapi_assistant_id = existing["vapi_assistant_id"]
    else:
        print(f"[provision] Creating new assistant for {business_slug}")
        assistant = vapi_client.create_assistant(config)
        vapi_assistant_id = assistant.get("id")
        if not vapi_assistant_id:
            raise ProvisionError(f"Vapi did not return assistant id: {assistant}")

    # 5+6. Νούμερο + σύνδεση με τον assistant. Σειρά προτεραιότητας:
    #   (α) re-route υπάρχοντος, (β) συγκεκριμένο Twilio number αν δόθηκε,
    #   (γ) αγορά ελληνικού Twilio αν ζητήθηκε & στημένο, (δ) reuse owned Twilio,
    #   (ε) DEFAULT TURNKEY: ΔΩΡΕΑΝ Vapi number (ΧΩΡΙΣ Twilio account) → 1-click setup.
    twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_phone_sid = None
    twilio_phone_number = twilio_phone
    vapi_phone_id = None
    sip_cred = sip_credential_id or os.environ.get("VAPI_SIP_CREDENTIAL_ID")

    if existing and existing.get("vapi_phone_id"):
        vapi_client.update_phone_assistant(existing["vapi_phone_id"], vapi_assistant_id)
        vapi_phone_id = existing["vapi_phone_id"]
        twilio_phone_number = twilio_phone_number or existing.get("twilio_phone_number")
        print(f"[provision] Re-routed existing Vapi phone {vapi_phone_id}")
    elif sip_number and sip_cred:
        # ΕΛΛΗΝΙΚΟ DID μέσω BYO SIP — production (μηδέν διεθνής χρέωση σε γιατρό/ασθενή)
        rec = vapi_client.attach_sip_number(sip_number, sip_cred, vapi_assistant_id,
                                            name=f"{business_slug}-gr")
        vapi_phone_id = rec.get("id")
        twilio_phone_number = sip_number
        print(f"[provision] Linked Greek SIP number {sip_number} (phone_id={vapi_phone_id})")
    elif sip_number and not sip_cred:
        raise ProvisionError(
            "Δόθηκε ελληνικός αριθμός αλλά λείπει το VAPI_SIP_CREDENTIAL_ID. "
            "Στήσε μία φορά SIP trunk credential στο Vapi (από τον πάροχο DID: Zadarma/Telnyx) "
            "και βάλε το credential id στο Railway.")
    elif twilio_phone:
        rec = vapi_client.attach_twilio_number(
            twilio_sid=twilio_sid, twilio_token=twilio_token,
            twilio_phone=twilio_phone, assistant_id=vapi_assistant_id,
            name=f"{business_slug}-line")
        vapi_phone_id = rec.get("id")
        print(f"[provision] Linked given Twilio {twilio_phone}")
    elif purchase_new_number and twilio_client.is_configured():
        available = twilio_client.search_available_greek_numbers(area_code, limit=5)
        if not available:
            raise ProvisionError(
                f"Δεν βρέθηκαν ελληνικά νούμερα για κωδικό {area_code}. "
                "Δοκίμασε άλλον κωδικό ή κάνε verification στο Twilio.")
        purchased = twilio_client.purchase_number(
            available[0]["phone_number"], friendly_name=f"Ran-Tevu {business_slug}")
        twilio_phone_number = purchased.get("phone_number")
        twilio_phone_sid = purchased.get("sid")
        rec = vapi_client.attach_twilio_number(
            twilio_sid=twilio_sid, twilio_token=twilio_token,
            twilio_phone=twilio_phone_number, assistant_id=vapi_assistant_id,
            name=f"{business_slug}-line")
        vapi_phone_id = rec.get("id")
        print(f"[provision] Purchased + linked Twilio {twilio_phone_number}")
    elif twilio_client.is_configured():
        owned = twilio_client.list_owned_numbers()
        if owned:
            twilio_phone_number = owned[0]["phone_number"]
            twilio_phone_sid = owned[0]["sid"]
            rec = vapi_client.attach_twilio_number(
                twilio_sid=twilio_sid, twilio_token=twilio_token,
                twilio_phone=twilio_phone_number, assistant_id=vapi_assistant_id,
                name=f"{business_slug}-line")
            vapi_phone_id = rec.get("id")
            print(f"[provision] Reused + linked Twilio {twilio_phone_number}")

    if not vapi_phone_id:
        # DEFAULT TURNKEY: δωρεάν Vapi number — ΧΩΡΙΣ Twilio account (1-click voice)
        rec = vapi_client.create_vapi_number(vapi_assistant_id, name=f"{business_slug}-line")
        vapi_phone_id = rec.get("id")
        twilio_phone_number = rec.get("number") or twilio_phone_number
        print(f"[provision] Created FREE Vapi number {twilio_phone_number} (phone_id={vapi_phone_id})")

    # 7. Save to DB
    _save_agent(business_slug, {
        "vapi_assistant_id": vapi_assistant_id,
        "vapi_phone_id": vapi_phone_id,
        "twilio_phone_sid": twilio_phone_sid,
        "twilio_phone_number": twilio_phone_number,
        "voice_key": voice_key,
        "assistant_name": assistant_name,
        "status": "active",
        "config": config,
    })

    return {
        "ok": True,
        "business_slug": business_slug,
        "vapi_assistant_id": vapi_assistant_id,
        "vapi_phone_id": vapi_phone_id,
        "twilio_phone_number": twilio_phone_number,
        "voice_key": voice_key,
        "assistant_name": assistant_name,
        "webhook_url": f"{os.environ.get('PUBLIC_BASE_URL', 'https://www.ran-tevu.gr')}"
                       f"/api/voice/webhook/{business_slug}",
        "next_steps": [
            f"Δοκίμασε καλώντας: {twilio_phone_number}",
            f"Παρακολούθησε calls: ran-tevu.gr/admin/{business_slug}/voice",
        ],
    }


def deprovision_voice_agent(business_slug: str,
                            release_twilio_number: bool = False) -> dict:
    """Διαγραφή voice agent. Optional release Twilio number (stops billing)."""
    existing = _get_existing_agent(business_slug)
    if not existing:
        return {"ok": False, "error": "No voice agent found for slug"}

    result = {"business_slug": business_slug, "actions": []}

    if existing.get("vapi_phone_id"):
        try:
            vapi_client.delete_phone_number(existing["vapi_phone_id"])
            result["actions"].append("vapi_phone_deleted")
        except Exception as e:
            result["actions"].append(f"vapi_phone_delete_failed: {e}")

    if existing.get("vapi_assistant_id"):
        try:
            vapi_client.delete_assistant(existing["vapi_assistant_id"])
            result["actions"].append("vapi_assistant_deleted")
        except Exception as e:
            result["actions"].append(f"vapi_assistant_delete_failed: {e}")

    if release_twilio_number and existing.get("twilio_phone_sid"):
        try:
            twilio_client.release_number(existing["twilio_phone_sid"])
            result["actions"].append("twilio_number_released")
        except Exception as e:
            result["actions"].append(f"twilio_release_failed: {e}")

    with db._conn() as c:
        c.execute("UPDATE voice_agents SET status='deactivated' WHERE business_slug=?",
                  (business_slug,))
        c.commit()
    result["actions"].append("db_marked_deactivated")
    result["ok"] = True
    return result


def get_voice_agent(business_slug: str) -> Optional[dict]:
    """Public getter — returns agent row or None."""
    return _get_existing_agent(business_slug)
