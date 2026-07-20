# Ran-Tevu Voice Agent System

Production-grade voice receptionist για ελληνικές επιχειρήσεις.

## Στοίβα

| Layer | Service | Cost |
|-------|---------|------|
| Telephony | **Twilio** Greek number | $1/μήνα + $0.013/min |
| Orchestration | **Vapi.ai** | $0.05/min |
| STT (Greek) | **Deepgram nova-2** (μέσω Vapi) | included |
| LLM | **Claude Sonnet 4.5** | ~$0.01/min |
| TTS (Greek) | **ElevenLabs multilingual** | $0.18/min |
| Backend | **Ran-Tevu Flask app** | $0 (Railway free) |

**Total cost: ~€0.23/min**. Πούλα €49-99/μήνα flat → margin 55-90%.

## Quick start

### 1. API keys στο .env (ή Railway env)
```bash
VAPI_API_KEY=...                    # vapi.ai/dashboard → API Keys
TWILIO_ACCOUNT_SID=AC...            # twilio.com/console
TWILIO_AUTH_TOKEN=...
ELEVENLABS_API_KEY=...              # elevenlabs.io/api
PUBLIC_BASE_URL=https://ran-tevu.gr # για webhooks
VAPI_WEBHOOK_SECRET=ran-tevu-voice-2026  # protect webhook
```

### 2. Healthcheck
```bash
python -m voice.cli healthcheck
```
Πρέπει να δείξει `"ok": true` και για τα 3.

### 3. Provision voice agent για πελάτη
```bash
# Αν έχεις ήδη Twilio number:
python -m voice.cli provision papadopoulos-clinic --phone +302810123456

# Αν θες να αγοράσει νέο Ρέθυμνο number:
python -m voice.cli provision papadopoulos-clinic --buy --area-code 2831

# Με συγκεκριμένη φωνή:
python -m voice.cli provision papadopoulos-clinic --voice dimitris --name "Δημήτρης"

# Dry run (δες config χωρίς API calls):
python -m voice.cli provision papadopoulos-clinic --dry-run
```

### 4. Admin UI
```
https://ran-tevu.gr/admin/papadopoulos-clinic/voice?token=ADMIN_TOKEN
```
- Configure voice + assistant name
- Live call log + transcripts
- Monthly cost breakdown
- Provision / deprovision

## Architecture

```
                                Webhook (function-call)
Customer → +30 281 0 XXXXXX  ──→  POST /api/voice/webhook/<slug>
                                  ↓
                                  webhook_handler.py
                                  ├→ check_availability  → database.get_available_slots
                                  ├→ book_appointment    → database.create_booking
                                  ├→ transfer_to_owner   → return {destination: ...}
                                  └→ send_sms            → twilio_client.send_sms
                                  ↓
                                  Vapi gets result → Claude generates next utterance
                                  → ElevenLabs TTS → Twilio → customer hears it
```

## Function calling (what Claude can do)

| Function | Trigger phrase | Action |
|----------|----------------|--------|
| `check_availability` | "Πότε έχει διαθέσιμη ώρα Παρασκευή;" | Returns slots |
| `book_appointment` | "Ναι, κλείστο" | Inserts booking + sends SMS |
| `transfer_to_owner` | "Πονάει πολύ" / "Επείγον" | Transfers to owner_phone |
| `send_sms_confirmation` | Auto post-booking | Confirms via SMS |

## Greek voices (ElevenLabs multilingual_v2)

| Voice | Gender | Tone | Best for |
|-------|--------|------|----------|
| `madlen` | F | Warm, friendly | medical, beauty, wellness, spa |
| `stefanos` | M | Professional, calm | legal, restaurant, real estate |
| `athina` | F | Young, energetic | fitness, hair, nails, yoga |
| `dimitris` | M | Mature, authoritative | dental, veterinary, physio |

Auto-selected by `pick_voice_for_business(business_type)` if not specified.

## System prompts (per industry)

Στο `voice/system_prompts.py`. Industries:
- `dental` — emergencies, pricing
- `beauty` — packages, treatments
- `hair` — color asking, services
- `vet` — animal type validation, emergency detection
- `physio` — assessment-first
- `restaurant` — party size first
- `fitness` — class booking
- `default` — generic fallback

## Webhook endpoint contract

Vapi POST `/api/voice/webhook/<slug>` με body:
```json
{
  "message": {
    "type": "function-call",
    "call": {"id": "..."},
    "functionCall": {
      "name": "check_availability",
      "parameters": {"date": "2026-05-22", "service": "καθαρισμός"}
    }
  }
}
```

Server responds:
```json
{"result": "Έχω διαθέσιμα: δέκα, δωδεκάμισι. Ποιο σας βολεύει;"}
```

End-of-call posts:
```json
{
  "message": {
    "type": "end-of-call-report",
    "call": {...},
    "artifact": {"transcript": "...", "recordingUrl": "..."},
    "analysis": {
      "summary": "Πελάτης έκλεισε καθαρισμό για Παρασκευή...",
      "structuredData": {"customer_name": "...", "booking_confirmed": true},
      "successEvaluation": "ΝΑΙ"
    },
    "cost": 0.18,
    "durationSeconds": 240
  }
}
```

Saved to `voice_calls` table automatically.

## Database tables

- `voice_agents` — one row per business with Vapi assistant + Twilio phone IDs
- `voice_calls` — every completed call with transcript, summary, cost
- `voice_call_events` — audit log of every webhook hit (debugging)

## Testing locally

```bash
# 1. Healthcheck all APIs
python -m voice.cli healthcheck

# 2. Test TTS (generates MP3)
python -m voice.cli test-tts "Καλημέρα! Είμαι η Madlen." --voice madlen

# 3. Dry-run provision (no API calls)
python -m voice.cli provision dental-demo --dry-run

# 4. Search available Greek numbers (Athens):
python -m voice.cli search-numbers --area-code 21

# 5. Live provision (uses APIs!):
python -m voice.cli provision dental-demo --buy
```

## Customer onboarding flow

1. **Πελάτης κλείνει deal** μέσω founding page / Cal.com / cold call
2. **Συμπληρώνει** business details (name, hours, services, phone) στο `/admin/<slug>/onboarding`
3. **Εσύ τρέχεις** `python -m voice.cli provision <slug> --buy`
4. **Σε 60 δευτερόλεπτα** ο πελάτης έχει working number
5. **Δίνεις το number** στον πελάτη + admin URL
6. **Πελάτης call-forward** το business number του στο Twilio number σου
7. **Done.** Όλες οι κλήσεις απαντώνται από AI 24/7.

## Cost economics ανά πελάτη

**Σενάριο A: 30 min κλήσεων/μήνα (small clinic)**
- Vapi: $1.50 + Twilio: $0.39 + Claude: $0.30 + ElevenLabs: $5.40 + Number: $1
- TOTAL κόστος σου: **$8.59/μήνα (~€7.90)**
- Charge €49/μήνα → margin €41 (84%)

**Σενάριο B: 120 min κλήσεων/μήνα (busy practice)**
- TOTAL κόστος σου: ~$30/μήνα (~€28)
- Charge €99/μήνα → margin €71 (72%)

**Σενάριο C: pay-per-booking (€3/booking)**
- 50 bookings/μήνα × €3 = €150 revenue
- 60 min calls cost ~€14
- Net margin €136 (90%)

## Production checklist

- [ ] `VAPI_API_KEY` στο Railway env
- [ ] `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` στο Railway env
- [ ] `ELEVENLABS_API_KEY` στο Railway env (Starter plan $5/μήνα recommended)
- [ ] `PUBLIC_BASE_URL=https://ran-tevu.gr` στο Railway env
- [ ] `VAPI_WEBHOOK_SECRET` (random string) στο Railway env
- [ ] Twilio Greek number purchased (or use trial number για testing)
- [ ] First provision tested: `python -m voice.cli provision test-slug --dry-run`
- [ ] Real provision: `python -m voice.cli provision <slug>`
- [ ] Test call placed to the Twilio number
- [ ] Admin UI checked: `/admin/<slug>/voice`
- [ ] First booking ended up στο bookings table με `notes` starting με `[VOICE]`
