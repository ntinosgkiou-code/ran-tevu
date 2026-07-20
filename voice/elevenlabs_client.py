"""
ElevenLabs TTS wrapper για Greek voices.

Δύο χρήσεις:
1. Real-time voice (Vapi κάνει το routing μόνο του — ορίζουμε voice_id)
2. Pre-generated samples (πχ greeting audio για web preview)

Greek voices που δουλεύουν καλά (tested):
- Madlen — γυναικεία, ζεστή, ιδανική για medical/wellness
- Stefanos — αντρική, professional, ιδανική για legal/restaurant
"""
import os
import requests
from typing import Optional

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"


# Voice IDs — όλα από το ElevenLabs voice library, multilingual
GREEK_VOICES = {
    "madlen": {
        "voice_id": "7smwXrU3C1PfaspIIUZB",   # Sophia — ΕΛΛΗΝΙΚΗ ζεστή γυναικεία call-center (research: #1 για ελληνικά· Sarah δεν ήταν ελληνική)
        "name": "Ράνια",
        "gender": "female",
        "tone": "warm, friendly",
        "best_for": ["medical", "beauty", "wellness", "spa", "physio", "dental", "vet"],
    },
    "stefanos": {
        "voice_id": "VR6AewLTigWG4xSOukaG",   # Arnold (multilingual, professional)
        "name": "Stefanos",
        "gender": "male",
        "tone": "professional, calm",
        "best_for": ["legal", "restaurant", "automotive", "real estate"],
    },
    "athina": {
        "voice_id": "EXAVITQu4vr4xnSDxMaL",   # Sarah (multilingual, νεανική)
        "name": "Athina",
        "gender": "female",
        "tone": "young, energetic",
        "best_for": ["fitness", "hair salon", "nails", "yoga"],
    },
    "dimitris": {
        "voice_id": "pNInz6obpgDQGcFmaJgB",   # Adam (multilingual, mature)
        "name": "Dimitris",
        "gender": "male",
        "tone": "mature, authoritative",
        "best_for": ["dental", "veterinary", "physio"],
    },
}


class ElevenLabsError(Exception):
    pass


def _headers() -> dict:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise ElevenLabsError("ELEVENLABS_API_KEY not configured")
    return {"xi-api-key": key}


def generate_speech(text: str, voice_key: str = "madlen",
                    output_path: Optional[str] = None,
                    model: str = "eleven_multilingual_v2") -> bytes:
    """Generate speech audio (MP3) από κείμενο.
    Returns audio bytes; optionally writes to output_path."""
    if voice_key not in GREEK_VOICES:
        raise ElevenLabsError(f"Unknown voice '{voice_key}'. "
                              f"Options: {list(GREEK_VOICES)}")
    voice_id = GREEK_VOICES[voice_key]["voice_id"]
    url = f"{ELEVENLABS_BASE}/text-to-speech/{voice_id}"

    payload = {
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability": 0.55,
            "similarity_boost": 0.85,
            "style": 0.3,
            "use_speaker_boost": True,
        },
    }
    headers = {**_headers(), "Content-Type": "application/json",
               "Accept": "audio/mpeg"}

    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 400:
        raise ElevenLabsError(f"ElevenLabs {r.status_code}: {r.text[:500]}")
    audio = r.content

    if output_path:
        with open(output_path, "wb") as f:
            f.write(audio)
    return audio


def get_voice_id(voice_key: str) -> str:
    """Resolve a friendly key to ElevenLabs voice_id (for Vapi config)."""
    if voice_key not in GREEK_VOICES:
        voice_key = "madlen"
    return GREEK_VOICES[voice_key]["voice_id"]


def pick_voice_for_business(business_type: str) -> str:
    """Auto-select best voice key for business type."""
    bt = (business_type or "").lower()
    for voice_key, meta in GREEK_VOICES.items():
        for tag in meta["best_for"]:
            if tag in bt:
                return voice_key
    return "madlen"  # default


def is_configured() -> bool:
    return bool(os.environ.get("ELEVENLABS_API_KEY"))


def healthcheck() -> dict:
    if not is_configured():
        return {"ok": False, "configured": False}
    try:
        r = requests.get(f"{ELEVENLABS_BASE}/user", headers=_headers(),
                         timeout=10)
        if r.status_code == 200:
            data = r.json()
            sub = data.get("subscription", {})
            return {
                "ok": True,
                "configured": True,
                "chars_used": sub.get("character_count", 0),
                "chars_limit": sub.get("character_limit", 0),
                "tier": sub.get("tier", "free"),
            }
        return {"ok": False, "configured": True,
                "error": f"{r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "configured": True, "error": str(e)}
