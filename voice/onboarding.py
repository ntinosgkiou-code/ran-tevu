"""
Self-service onboarding orchestrator.

Παίρνει form submission από νέο πελάτη και:
1. Generates unique slug
2. Creates business σε DB με όλες τις voice settings
3. Provisions voice agent (Vapi + Twilio + ElevenLabs)
4. Sends owner email με phone number + admin URL
5. Returns success dict για το frontend

Single function για να το καλέσει το /api/onboarding/voice/submit endpoint.
"""
import os
import re
import secrets
import json
from typing import Optional
import database as db


GREEK_TO_LATIN = {
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i",
    "θ": "th", "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x",
    "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y",
    "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o",
    "ά": "a", "έ": "e", "ή": "i", "ί": "i", "ό": "o", "ύ": "y", "ώ": "o",
    "ϊ": "i", "ϋ": "y", "ΐ": "i", "ΰ": "y",
}


def slugify(name: str) -> str:
    """Convert Greek/English business name to URL slug."""
    s = (name or "").lower().strip()
    out = ""
    for ch in s:
        out += GREEK_TO_LATIN.get(ch, ch)
    out = re.sub(r"[^a-z0-9-]+", "-", out)
    out = re.sub(r"-+", "-", out).strip("-")
    return out[:60] or f"business-{secrets.token_hex(3)}"


def unique_slug(base: str) -> str:
    """Adds suffix if base already exists."""
    existing = db.get_business(base)
    if not existing:
        return base
    for i in range(2, 100):
        cand = f"{base}-{i}"
        if not db.get_business(cand):
            return cand
    return f"{base}-{secrets.token_hex(3)}"


def onboard_new_business(payload: dict, provision_voice: bool = True) -> dict:
    """Main entry: παίρνει form payload, δημιουργεί business + provisions voice agent.

    Expected payload:
    {
        "business_name": "Δρ. Παπαδόπουλος Οδοντιατρείο",
        "business_type": "οδοντιατρείο",
        "owner_name": "Δρ. Νίκος Παπαδόπουλος",
        "owner_email": "drpapas@example.com",
        "owner_phone": "+30210...",   # για transfer-to-owner
        "address": "Κηφισίας 100, Αθήνα",
        "hours": {"monday": "09:00-18:00", ...},
        "services": [{"name": "...", "duration": 30, "price": 50}, ...],
        "voice_key": "madlen",
        "voice_assistant_name": "Madlen",
        "voice_tone": "friendly",
        "voice_custom_instructions": "Πες πάντα ότι ...",
        "voice_greeting": "Καλημέρα! ...",   # optional, else auto
        "twilio_phone": "+30...",       # optional
        "purchase_new_number": false,
        "area_code": "2831",
    }
    """
    # ─ Validate basics ─
    name = (payload.get("business_name") or "").strip()
    email = (payload.get("owner_email") or "").strip().lower()
    if not name:
        return {"ok": False, "error": "business_name required"}
    if not email or "@" not in email:
        return {"ok": False, "error": "Valid owner_email required"}

    # ─ Generate slug ─
    base_slug = slugify(name)
    slug = unique_slug(base_slug)

    # ─ Build business config ─
    services = payload.get("services") or []
    if isinstance(services, str):
        try:
            services = json.loads(services)
        except Exception:
            services = []

    hours = payload.get("hours") or {
        "monday": "09:00-18:00", "tuesday": "09:00-18:00",
        "wednesday": "09:00-18:00", "thursday": "09:00-18:00",
        "friday": "09:00-18:00",
    }
    if isinstance(hours, str):
        try:
            hours = json.loads(hours)
        except Exception:
            hours = {}

    business_config = {
        "name": name,
        "type": payload.get("business_type") or "επιχείρηση",
        "tagline": payload.get("tagline") or "",
        "phone": payload.get("owner_phone") or "",
        "address": payload.get("address") or "",
        "owner_email": email,
        "owner_name": payload.get("owner_name") or "",
        "hours": json.dumps(hours, ensure_ascii=False),
        "services": json.dumps(services, ensure_ascii=False),
        "slot_minutes": int(payload.get("slot_minutes", 30)),
        "day_start": payload.get("day_start", "09:00"),
        "day_end": payload.get("day_end", "20:00"),
        # Voice fields
        "voice_key": payload.get("voice_key", "madlen"),
        "voice_assistant_name": payload.get("voice_assistant_name", "Madlen"),
        "voice_tone": payload.get("voice_tone", "friendly"),
        "voice_custom_instructions": (payload.get("voice_custom_instructions") or "")[:5000],
        "voice_greeting": (payload.get("voice_greeting") or "")[:500],
    }

    # ─ Create business in DB ─
    try:
        biz_id = db.create_business(slug, business_config)
    except Exception as e:
        return {"ok": False, "error": f"DB create failed: {e}"}

    result = {
        "ok": True,
        "business_slug": slug,
        "business_id": biz_id,
        "admin_url": f"{os.environ.get('PUBLIC_BASE_URL', 'https://www.ran-tevu.gr')}"
                     f"/admin/{slug}/voice?token=set-this-token",
        "chat_url": f"{os.environ.get('PUBLIC_BASE_URL', 'https://www.ran-tevu.gr')}/{slug}",
        "voice_provisioned": False,
    }

    # ─ Provision voice agent (if APIs configured) ─
    if not provision_voice:
        result["provision_skipped_reason"] = "provision_voice=False"
        return result

    try:
        from voice.provisioner import provision_voice_agent, ProvisionError
        from voice import vapi_client, twilio_client

        if not vapi_client.is_configured():
            result["provision_skipped_reason"] = "VAPI_API_KEY not set"
            result["next_step"] = "Set VAPI_API_KEY in Railway env, then re-provision via admin UI"
            return result

        provision_result = provision_voice_agent(
            business_slug=slug,
            voice_key=business_config["voice_key"],
            assistant_name=business_config["voice_assistant_name"],
            twilio_phone=payload.get("twilio_phone"),
            purchase_new_number=bool(payload.get("purchase_new_number", False)),
            area_code=payload.get("area_code", "2831"),
        )
        result["voice_provisioned"] = True
        result["voice_phone_number"] = provision_result.get("twilio_phone_number")
        result["vapi_assistant_id"] = provision_result.get("vapi_assistant_id")

    except ProvisionError as e:
        result["voice_provisioned"] = False
        result["provision_error"] = str(e)
        result["next_step"] = (
            f"Business created but voice not yet active. Fix: {e}. "
            f"Re-provision via /admin/{slug}/voice."
        )
    except Exception as e:
        result["voice_provisioned"] = False
        result["provision_error"] = f"Unexpected: {e}"

    # ─ Send owner email ─
    try:
        _send_onboarding_email(email, result, business_config)
    except Exception as e:
        result["email_warning"] = f"email send failed: {e}"

    return result


def _send_onboarding_email(email: str, result: dict, biz_config: dict):
    """Welcome email με phone number + admin URL + getting-started."""
    try:
        from email_service import send_email
    except ImportError:
        print(f"[onboarding] email_service not available — skip welcome email")
        return

    business_name = biz_config["name"]
    slug = result["business_slug"]
    live = bool(result.get("voice_provisioned"))
    phone = result.get("voice_phone_number") or ""
    admin = result.get("admin_url")
    chat = result.get("chat_url")
    assistant = biz_config.get("voice_assistant_name", "Άννα")

    # ΕΙΛΙΚΡΙΝΕΙΑ: «live» ΜΟΝΟ αν η φωνή στήθηκε όντως· αλλιώς «λογαριασμός έτοιμος».
    if live and phone:
        subject = f"🎉 Η τηλεφωνική σας AI είναι live — {business_name}"
        header = "🎉 Είστε live!"
        voice_block = f"""
      <p>Η AI γραμματεία σας <strong>{assistant}</strong> απαντάει τώρα 24/7 για το <strong>{business_name}</strong>.</p>
      <h2>📞 Το νούμερό σας</h2>
      <div style="background:#f3f4f6;padding:16px;border-radius:8px;font-size:24px;font-weight:bold;text-align:center;">{phone}</div>
      <h2>🚀 Πώς το συνδέετε με το υπάρχον τηλέφωνό σας</h2>
      <ol>
        <li>Ζητήστε από τον πάροχό σας (Cosmote/Vodafone/Nova) <strong>προώθηση κλήσης</strong> (busy/no-answer) → <strong>{phone}</strong></li>
        <li>Δοκιμάστε καλώντας το δικό σας νούμερο — θα απαντήσει η {assistant}!</li>
      </ol>"""
    else:
        subject = f"✅ Ο λογαριασμός σας στο Ran-Tevu είναι έτοιμος — {business_name}"
        header = "✅ Ο λογαριασμός σας είναι έτοιμος"
        voice_block = """
      <p>Ο λογαριασμός σας δημιουργήθηκε. Το <strong>web chat</strong> δουλεύει ήδη (πιο κάτω).</p>
      <h2>📞 Η τηλεφωνική AI</h2>
      <p>Ενεργοποιείται με <strong>ένα κλικ</strong> από τον πίνακα διαχείρισης («Στήσε φωνή»). Μόλις είναι έτοιμο το νούμερό σας, θα το δείτε εκεί.</p>"""

    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:600px;margin:auto;padding:20px;">
      <h1 style="color:#7c3aed">{header}</h1>
      <p>Καλημέρα,</p>
      {voice_block}

      <h2>🎛️ Πίνακας διαχείρισης</h2>
      <p>Δείτε κλήσεις, ραντεβού, ασθενείς:<br>
      <a href="{admin}">{admin}</a></p>

      <h2>💬 Web chat URL</h2>
      <p>Βάλτε αυτό το link στο site/Instagram σας:<br>
      <a href="{chat}">{chat}</a></p>

      <p style="margin-top:30px;color:#6b7280;font-size:13px;">
        Αν έχετε ερωτήσεις, απαντήστε σε αυτό το email.<br>
        — Ran-Tevu
      </p>
    </div>
    """

    send_email(
        to=email,
        subject=subject,
        html=html,
        text=f"Καλώς ήρθατε στο Ran-Tevu! Το νούμερό σας: {phone}. Admin: {admin}",
    )
