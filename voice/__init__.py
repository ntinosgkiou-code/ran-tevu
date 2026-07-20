"""
Ran-Tevu Voice Agent System
============================
Production-grade voice receptionist για ελληνικές επιχειρήσεις.

Stack:
- Twilio: Greek phone number (telephony)
- Vapi.ai: conversation orchestration + STT
- ElevenLabs: Greek TTS (Madlen, Stefanos)
- Claude (Sonnet 4.5): LLM brain
- Ran-Tevu app.py: booking actions + DB

Architecture:
    Customer → Twilio number → Vapi → [Claude ↔ Webhooks ↔ DB]
                                  └→ ElevenLabs → audio back to customer

Module map:
- vapi_client.py        — REST API wrapper για Vapi
- twilio_client.py      — Twilio number management
- elevenlabs_client.py  — voice generation + voice library
- system_prompts.py     — Greek prompts per business type
- assistant_builder.py  — Build Vapi assistant config from a business
- provisioner.py        — End-to-end: business → working voice agent
- webhook_handler.py    — Vapi webhook handlers (function calls, end-of-call)
- cost_tracker.py       — Per-call cost tracking
"""

from voice.provisioner import provision_voice_agent, deprovision_voice_agent
from voice.webhook_handler import handle_vapi_webhook
from voice.assistant_builder import build_assistant_config

__all__ = [
    "provision_voice_agent",
    "deprovision_voice_agent",
    "handle_vapi_webhook",
    "build_assistant_config",
]
