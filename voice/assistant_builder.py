"""
Build full Vapi assistant configuration από business data.

Output είναι JSON-serializable dict έτοιμο για POST /assistant.
Περιλαμβάνει:
- model (Claude Sonnet 4.5 via Anthropic provider)
- voice (ElevenLabs Greek voice)
- transcriber (Deepgram Greek STT)
- functions (check_availability, book_appointment, transfer_to_owner, send_sms_link)
- serverUrl (webhook back to ran-tevu.gr)
- recording + transcription enabled
"""
import os
from voice.system_prompts import (
    build_system_prompt,
    build_first_message,
    build_end_call_message,
)
from voice.elevenlabs_client import get_voice_id, pick_voice_for_business


def _webhook_base() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "https://www.ran-tevu.gr").rstrip("/")


def build_functions(business_slug: str) -> list:
    """Function calls που μπορεί να κάνει το AI κατά τη διάρκεια κλήσης."""
    return [
        {
            "name": "check_availability",
            "description": (
                "Έλεγξε αν υπάρχει διαθέσιμη ώρα για ραντεβού σε συγκεκριμένη "
                "ημερομηνία. Επιστρέφει λίστα διαθέσιμων ωρών."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Ημερομηνία σε format YYYY-MM-DD (π.χ. '2026-05-22')",
                    },
                    "service": {
                        "type": "string",
                        "description": (
                            "Όνομα υπηρεσίας (π.χ. 'καθαρισμός', 'βαφή', "
                            "'εξέταση'). Αν δεν είναι σαφές, χρησιμοποίησε 'general'."
                        ),
                    },
                },
                "required": ["date", "service"],
            },
        },
        {
            "name": "book_appointment",
            "description": (
                "Κλείσε επιβεβαιωμένο ραντεβού. ΧΡΗΣΙΜΟΠΟΙΗΣΕ ΜΟΝΟ μετά από "
                "επιβεβαίωση του πελάτη ('ναι κλείστο')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time": {"type": "string", "description": "HH:MM (24h)"},
                    "service": {"type": "string"},
                    "customer_name": {"type": "string"},
                    "customer_phone": {
                        "type": "string",
                        "description": "Ελληνικό τηλέφωνο, π.χ. '6987747434'",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Προαιρετικές σημειώσεις από τον πελάτη",
                    },
                },
                "required": ["date", "time", "service", "customer_name", "customer_phone"],
            },
        },
        {
            "name": "add_to_waitlist",
            "description": (
                "Βάλε τον πελάτη στη ΛΙΣΤΑ ΑΝΑΜΟΝΗΣ όταν η ώρα/μέρα που θέλει είναι ΓΕΜΑΤΗ, ή όταν "
                "το ιατρείο είναι κλειστό και θέλει 'την πρώτη διαθέσιμη'. Μόλις κάποιος ακυρώσει, "
                "ειδοποιείται αυτόματα με SMS. Ρώτα ημερομηνία που προτιμά, όνομα και τηλέφωνο."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Προτιμώμενη ημερομηνία YYYY-MM-DD"},
                    "time": {"type": "string", "description": "Προτιμώμενη ώρα HH:MM αν υπάρχει, αλλιώς παράλειψε"},
                    "service": {"type": "string"},
                    "customer_name": {"type": "string"},
                    "customer_phone": {"type": "string", "description": "Ελληνικό τηλέφωνο"},
                },
                "required": ["date", "service", "customer_name", "customer_phone"],
            },
        },
        {
            "name": "lookup_appointment",
            "description": (
                "Βρες τα επερχόμενα ραντεβού ΥΠΑΡΧΟΝΤΟΣ πελάτη με βάση το τηλέφωνό του. "
                "Χρησιμοποίησέ το όταν ο πελάτης ρωτάει «πότε έχω ραντεβού;», θέλει να "
                "επιβεβαιώσει, να αλλάξει ή να ακυρώσει — πρώτα δες τι έχει."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_phone": {
                        "type": "string",
                        "description": "Τηλέφωνο πελάτη, π.χ. '6987747434'",
                    },
                },
                "required": ["customer_phone"],
            },
        },
        {
            "name": "transfer_to_owner",
            "description": (
                "Μετάφερε την κλήση στον ιδιοκτήτη/γιατρό. Χρησιμοποίησε όταν: "
                "(1) πελάτης ζητάει ιατρική/τεχνική συμβουλή, (2) είναι "
                "αναστατωμένος, (3) είναι επείγον."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Σύντομη αιτιολόγηση (π.χ. 'επείγον', 'παράπονο')",
                    },
                },
                "required": ["reason"],
            },
        },
        {
            "name": "send_sms_confirmation",
            "description": (
                "Στείλε SMS επιβεβαίωσης ραντεβού στον πελάτη. Καλείται "
                "αυτόματα μετά από επιτυχημένο book_appointment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_phone": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["customer_phone", "message"],
            },
        },
    ]


def build_assistant_config(business: dict, voice_key: str = None,
                          assistant_name: str = "Madlen") -> dict:
    """Πλήρες Vapi assistant config για ένα business.

    business: dict με keys name, slug, type, address, hours, services κλπ
    voice_key: 'madlen' / 'stefanos' / 'athina' / 'dimitris' (auto αν None)
    assistant_name: το όνομα που θα λέει η AI για τον εαυτό της
    """
    if voice_key is None:
        voice_key = pick_voice_for_business(business.get("type", ""))

    voice_id = get_voice_id(voice_key)
    base = _webhook_base()
    slug = business.get("slug", "default")

    system_prompt = build_system_prompt(business, assistant_name=assistant_name)
    first_msg = build_first_message(business, assistant_name=assistant_name)

    return {
        "name": (f"Ran-Tevu · {business.get('name') or slug}")[:40],
        "firstMessage": first_msg,
        "firstMessageMode": "assistant-speaks-first",
        "endCallMessage": build_end_call_message(),
        "endCallFunctionEnabled": True,

        # Maximum call duration in seconds (10 min — μετά κόβει για cost control)
        "maxDurationSeconds": 600,

        # Silence timeout (δίνει χρόνο στον ασθενή να σκεφτεί — 8s έκοβε απότομα)
        "silenceTimeoutSeconds": 30,

        # ── ΦΥΣΙΚΗ ΣΥΝΟΜΙΛΙΑ (turn-taking σαν άνθρωπος) ──────────────
        # Smart endpointing: καταλαβαίνει πότε ΟΝΤΩΣ τελείωσε ο ασθενής (δεν κόβει στη μέση)
        "startSpeakingPlan": {
            "waitSeconds": 0.6,   # ελληνικά: μην κόβεις τον ασθενή (0.4 κόβει στη μέση πρότασης)
            "smartEndpointingPlan": {"provider": "vapi"},   # recommended for non-English (ΟΧΙ livekit/English-tuned)
            "transcriptionEndpointingPlan": {
                "onPunctuationSeconds": 0.3,
                "onNoPunctuationSeconds": 1.0,   # 1.3→1.0: γρηγορότερο turn-taking (λιγότερο «νεκρός χρόνος») χωρίς να κόβει
                "onNumberSeconds": 0.5,
            },
        },
        # Ο ασθενής μπορεί να ΔΙΑΚΟΨΕΙ την AI μιλώντας (σαν σε άνθρωπο)
        "stopSpeakingPlan": {
            "numWords": 2,
            "voiceSeconds": 0.2,
            "backoffSeconds": 1.0,
            # ελληνικά: όταν ο πελάτης λέει «ναι/εντάξει» να ΜΗΝ κόβεται το bot νομίζοντας ότι το διακόπτουν
            "acknowledgementPhrases": ["ναι", "εντάξει", "οκ", "μμ", "μμχμ", "κατάλαβα", "ωραία", "μάλιστα"],
        },
        # built-in backchannels = αγγλικά «yeah/uh-huh» → ξενικό· τα ελληνικά «ναι/μάλιστα» τα κάνει το prompt
        "backchannelingEnabled": False,
        "backgroundDenoisingEnabled": True,
        "backgroundSound": "office",   # ελαφρύ ambient ρεσεψιόν → ακούγεται λιγότερο «demo»

        # ── Model (LLM) ──────────────────────────────────────────────
        "model": {
            # gpt-4o: Vapi-native (managed — ΔΕΝ θέλει δικό σου key) → μηδέν provider-fault.
            # Το claude-3-5-sonnet ΣΚΑΖΕ live (endedReason=providerfault-anthropic-llm-failed,
            # 5/12 κλήσεις) γιατί το Vapi δεν είχε Anthropic key. gpt-4o = άριστα ελληνικά + tool-calling.
            "provider": "openai",
            "model": "gpt-4.1",   # πιο έξυπνο instruction-following από gpt-4o → λιγότερα robotic loops, καταλαβαίνει νόημα
            "temperature": 0.4,    # λίγο πιο φυσικό/ανθρώπινο (όχι μηχανικό)
            "maxTokens": 250,    # σύντομες απαντήσεις γραμματέα (150 ήταν στενό για ελληνικά+tools)
            # ΚΡΙΣΙΜΟ: το Vapi θέλει messages[], ΟΧΙ systemPrompt (το αγνοούσε σιωπηλά →
            # η AI έτρεχε ΧΩΡΙΣ οδηγίες → έλεγε «μισό λεπτό» και έκλεινε χωρίς κράτηση).
            "messages": [{"role": "system", "content": system_prompt}],
            # ΚΡΙΣΙΜΟ: κάθε tool ΠΡΕΠΕΙ να έχει server.url + async:false ώστε το Vapi να στείλει το
            # tool-call στο webhook ΚΑΙ να ΠΕΡΙΜΕΝΕΙ το result (αλλιώς το book_appointment δεν φτάνει ποτέ).
            "tools": [
                {"type": "function", "function": fn, "async": False,
                 "server": {"url": f"{base}/api/voice/webhook/{slug}",
                            "secret": os.environ.get("VAPI_WEBHOOK_SECRET", "ran-tevu-voice-2026"),
                            # μέγιστη υπομονή· το book_appointment απαντάει τώρα <1s (side-effects σε
                            # background thread) — αλλά κρατάμε το timeout ψηλά για ασφάλεια.
                            "timeoutSeconds": 20}}
                for fn in build_functions(slug)
            ],
        },

        # ── Voice (TTS) ──────────────────────────────────────────────
        "voice": {
            "provider": "11labs",
            "voiceId": voice_id,
            # eleven_multilingual_v2 = πιο ΑΝΘΡΩΠΙΝΗ/φυσική ελληνική προφορά (το flash ακουγόταν ρομποτικό &
            # μπέρδευε ονόματα «Ρεθήμο Ρεθύμνου»). Λίγο πιο αργό αλλά ΦΥΣΙΚΟ — ο Ντίνος θέλει ανθρώπινο, όχι demo.
            "model": "eleven_multilingual_v2",
            "stability": 0.45,               # λίγο χαμηλά = πιο ζωντανό/ανθρώπινο (όχι μονότονο)
            "similarityBoost": 0.80,
            "style": 0.10,                   # ελαφριά εκφραστικότητα, χωρίς υπερβολή
            "useSpeakerBoost": False,
            "optimizeStreamingLatency": 3,
        },

        # ── Transcriber (STT) ────────────────────────────────────────
        "transcriber": {
            "provider": "deepgram",
            "model": "nova-2",
            "language": "el",     # Greek
            "smartFormat": True,
            "endpointing": 300,   # ms silence = end of utterance
        },

        # ── Server / Webhooks ────────────────────────────────────────
        # Νέο Vapi schema (assistant-level server) + legacy serverUrl για συμβατότητα
        "server": {"url": f"{base}/api/voice/webhook/{slug}",
                   "secret": os.environ.get("VAPI_WEBHOOK_SECRET", "ran-tevu-voice-2026")},
        "serverUrl": f"{base}/api/voice/webhook/{slug}",
        "serverUrlSecret": os.environ.get("VAPI_WEBHOOK_SECRET", "ran-tevu-voice-2026"),
        "serverMessages": [
            "function-call",
            "tool-calls",          # νέο Vapi format (model.tools) — αλλιώς ρίσκο να μη φτάνουν τα tool invocations → σιωπηλά failed bookings
            "end-of-call-report",
            "status-update",
            "transfer-destination-request",
        ],

        # ── Recording & analysis ─────────────────────────────────────
        # OFF: ιατρικές κλήσεις = health data (GDPR Art.9). Audio recording χωρίς ρητή
        # συναίνεση = νομικό ρίσκο. Το transcript/analysis παραμένουν (transcriber), που αρκούν.
        # Ενεργοποίησέ το ΜΟΝΟ με consent flow («η κλήση καταγράφεται…»).
        "recordingEnabled": False,
        "hipaaEnabled": False,    # Set True για US healthcare (πιο αυστηρά)

        # ── Voicemail detection ──────────────────────────────────────
        # OFF: εισερχόμενη γραμμή ρεσεψιόν (άνθρωπος καλεί), ΟΧΙ outbound → το detection
        # απλά πρόσθετε ρίσκο να παρερμηνεύσει την κλήση ως τηλεφωνητή.
        "voicemailDetection": {
            "provider": "twilio",
            "voicemailDetectionTypes": ["machine_end_beep", "machine_end_silence"],
            "enabled": False,
        },

        # ── Analysis (μετά την κλήση) ───────────────────────────────
        "analysisPlan": {
            "summaryPrompt": (
                "Συνόψισε τη συνομιλία σε 2-3 ελληνικές προτάσεις. "
                "Τι ζήτησε ο πελάτης; Τι έκλεισε;"
            ),
            "structuredDataPrompt": (
                "Εξάγαγε: customer_name, customer_phone, appointment_date, "
                "appointment_time, service_requested, booking_confirmed (bool), "
                "transferred_to_human (bool), customer_sentiment (positive/neutral/negative)."
            ),
            "successEvaluationPrompt": (
                "Πέτυχε ο σκοπός της κλήσης (booking, info ή transfer); "
                "Απάντησε ΝΑΙ ή ΟΧΙ και γιατί."
            ),
        },

        # Metadata για ranking & filtering στο dashboard
        "metadata": {
            "business_slug": slug,
            "business_type": business.get("type", ""),
            "ran_tevu_version": "1.0",
        },
    }


def build_test_assistant() -> dict:
    """Minimal test assistant για να επιβεβαιώσεις ότι API key δουλεύει."""
    return {
        "name": "Ran-Tevu Test Assistant",
        "firstMessage": "Γεια σας, αυτό είναι ένα τεστ. Πείτε κάτι.",
        "endCallMessage": "Ευχαριστώ, το τεστ τελείωσε.",
        "maxDurationSeconds": 60,
        "model": {
            # openai/gpt-4.1: Vapi-native, ίδιο με το production build_assistant_config. Το
            # claude-3-5-haiku-20241022 είναι RETIRED (404) — Vapi δεν έχει Anthropic key configured.
            "provider": "openai",
            "model": "gpt-4.1",
            "messages": [{"role": "system", "content": "Είσαι test assistant. Πες μία σύντομη απάντηση στα ελληνικά."}],
            "maxTokens": 100,
        },
        "voice": {
            "provider": "11labs",
            "voiceId": get_voice_id("madlen"),
            "model": "eleven_multilingual_v2",
        },
        "transcriber": {
            "provider": "deepgram",
            "model": "nova-2",
            "language": "el",
        },
    }
