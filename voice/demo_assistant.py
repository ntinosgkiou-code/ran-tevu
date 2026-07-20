"""
Demo voice assistant για in-browser preview στο homepage.

ΕΝΑΣ Vapi assistant που χρησιμοποιείται σε ΟΛΟΥΣ τους web demos.
Δεν συνδέεται με Twilio number — μόνο Web SDK (visitor's mic + speakers).

Cost control:
- 60-second hard limit per call
- "Demo only" prompt αποφεύγει expensive multi-turn conversations
- Visitor βλέπει timer + reminder

Provisioning:
    python -m voice.cli provision-demo
"""
import os
from voice import vapi_client
from voice.elevenlabs_client import get_voice_id


DEMO_ASSISTANT_NAME_ENV = "VAPI_DEMO_ASSISTANT_ID"


def build_demo_config() -> dict:
    """Special demo config — generic Greek dental clinic for preview only."""
    system_prompt = """
Είσαι η Έλενα, AI ρεσεψιονίστ του φυσικοθεραπευτηρίου "Φυσιοθεραπεία Plus".
Αυτό είναι ένα DEMO που εμφανίζεται σε φυσικοθεραπευτές/γιατρούς που σκέφτονται να σε αγοράσουν.

Ο ΣΤΟΧΟΣ ΣΟΥ ΣΕ ΑΥΤΗ ΤΗ DEMO ΣΥΝΟΜΙΛΙΑ:
1. Δείξε πόσο φυσικά μιλάς ελληνικά
2. Δείξε ότι μπορείς να ακούσεις και να καταλάβεις
3. Πρότεινε ραντεβού όταν σου ζητήσουν
4. Στο τέλος (~50 δευτερόλεπτα), πες:
   "Αυτό ήταν το demo! Αν θέλετε να έχετε εμένα στο ιατρείο σας,
    δείτε πιο κάτω. Ευχαριστώ που με δοκιμάσατε!"

ΥΠΗΡΕΣΙΕΣ Φυσιοθεραπεία Plus (demo data):
- Αρχική αξιολόγηση & πλάνο: 45 λεπτά, €40
- Συνεδρία φυσικοθεραπείας: 45 λεπτά, €35
- Χειροθεραπεία (manual therapy): 30 λεπτά, €30
- Laser / TECAR θεραπεία: 20 λεπτά, €25
- Θεραπευτική άσκηση / αποκατάσταση: 45 λεπτά, €35

ΩΡΑΡΙΟ: Δευτέρα-Παρασκευή 09:00-21:00, Σάββατο 09:00-14:00

ΚΑΝΟΝΕΣ:
- Μίλα ΣΥΝΤΟΜΑ — μέγιστο 2 προτάσεις/απάντηση
- Πιάσε ονόματα μέρες με ελληνικό format ("Παρασκευή 22 Μαΐου")
- Πιάσε ώρες με ελληνικό format ("τέσσερις και μισή το απόγευμα")
- ΜΗΝ δίνεις ποτέ ιατρικές/θεραπευτικές συμβουλές — μόνο ραντεβού & πληροφορίες
- Αν ο χρήστης ρωτήσει "είσαι AI;" → "Ναι, είμαι AI ρεσεψιονίστ. Μιλάμε σωστά;"
- Αν ζητήσει να κλείσει ραντεβού, ΠΡΟΣΠΟΙΗΣΟΥ ότι το κλείνεις (demo data, μη σώσεις).
"""

    return {
        "name": "Ran-Tevu PUBLIC DEMO (physio homepage)",
        "firstMessage": (
            "Καλησπέρα! Είμαι η Έλενα από το φυσικοθεραπευτήριο. "
            "Αυτό είναι ένα demo — δοκιμάστε να μου ζητήσετε ραντεβού!"
        ),
        "firstMessageMode": "assistant-speaks-first",
        "endCallMessage": "Ευχαριστώ που με δοκιμάσατε. Καλή σας μέρα!",
        "endCallFunctionEnabled": True,

        # CRITICAL: hard cap στα 90 δευτερόλεπτα για cost control
        "maxDurationSeconds": 90,
        "silenceTimeoutSeconds": 6,
        "responseDelaySeconds": 0.3,

        "model": {
            # openai/gpt-4.1: Vapi-native (managed — ΔΕΝ θέλει δικό σου key), ίδιο με το production
            # assistant_builder.py. Το claude-3-7-sonnet-20250219 είναι RETIRED (404) — Vapi δεν έχει
            # ούτε Anthropic key configured (θα έσκαγε με providerfault-anthropic-llm-failed).
            "provider": "openai",
            "model": "gpt-4.1",
            "temperature": 0.6,
            "maxTokens": 200,
            # Vapi θέλει messages[], ΟΧΙ systemPrompt (το αγνοεί σιωπηλά → AI χωρίς οδηγίες)
            "messages": [{"role": "system", "content": system_prompt}],
            # No function calls στο demo — όλα είναι playacting
        },

        "voice": {
            "provider": "11labs",
            "voiceId": get_voice_id("madlen"),
            "model": "eleven_multilingual_v2",
            "stability": 0.55,
            "similarityBoost": 0.85,
            "style": 0.3,
            "useSpeakerBoost": True,
            "optimizeStreamingLatency": 3,
        },

        "transcriber": {
            "provider": "deepgram",
            "model": "nova-2",
            "language": "el",
            "smartFormat": True,
            "endpointing": 300,
        },

        "recordingEnabled": False,  # Don't save visitor recordings
        "metadata": {
            "type": "public_demo",
            "ran_tevu_version": "1.0",
        },
    }


def provision_demo() -> dict:
    """Create or update the demo assistant. Stores id where convenient."""
    if not vapi_client.is_configured():
        return {"ok": False, "error": "VAPI_API_KEY not set"}

    existing_id = os.environ.get(DEMO_ASSISTANT_NAME_ENV)
    config = build_demo_config()

    if existing_id:
        try:
            result = vapi_client.update_assistant(existing_id, config)
            return {
                "ok": True,
                "action": "updated",
                "assistant_id": existing_id,
                "next_step": f"Set in Railway env: {DEMO_ASSISTANT_NAME_ENV}={existing_id}",
            }
        except Exception as e:
            print(f"[demo] update failed ({e}), creating new")

    try:
        result = vapi_client.create_assistant(config)
        new_id = result.get("id")
        return {
            "ok": True,
            "action": "created",
            "assistant_id": new_id,
            "next_step": (
                f"IMPORTANT: set this in Railway env so the homepage demo works:\n"
                f"  {DEMO_ASSISTANT_NAME_ENV}={new_id}"
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_demo_assistant_id() -> str | None:
    """Read the configured demo assistant id from env."""
    return os.environ.get(DEMO_ASSISTANT_NAME_ENV)
