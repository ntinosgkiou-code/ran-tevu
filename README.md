# Ran-Tevu

![status](https://img.shields.io/badge/status-portfolio%20project%2C%20offline%20since%20October%202026-lightgrey)

AI booking and back-office platform for Greek doctors and clinics. Patients book through an AI chat
instead of calling; the clinic gets a secretary-style back office with patient files, invoicing with
Greek VAT rules, no-show recovery and a revenue dashboard.

> **Status: portfolio project.** Ran-Tevu was deployed on Railway and offered to Greek clinics as a
> pilot. It did not reach paying customers and was taken offline in October 2026. The source is
> published as a record of the work, not as a running service. Nothing here has been re-run for
> this update.

About 39,500 lines of Python, 64 test files, built and directed by one person (architecture,
security review and shipping decisions are mine; implementation was done through AI-assisted
engineering).

> **On language:** these READMEs are in English, and code (function and variable names,
> architecture) is English throughout. The patient- and doctor-facing screens and some code
> comments are in Greek, because the target market was Greek clinics.

This repository is a source snapshot: secrets, patient data and internal business documents are
intentionally excluded (see `.gitignore`).

## What it does

- **Patient-facing booking:** an AI chat (Anthropic Claude) understands natural-language requests,
  checks real availability, and books the appointment. No phone call required.
- **Back office for the clinic:** day view with check-in, patient folders with encrypted clinical
  notes, invoicing (automatic Greek VAT and medical exemption), payments, inventory, and a "who owes
  me" ledger.
- **No-show recovery:** an autonomous loop detects no-shows and dormant patients and proposes
  win-back outreach; every action is consent-gated and audit-logged.
- **Multi-channel:** SMS (Twilio), email (Resend), and an AI voice receptionist (Vapi + ElevenLabs)
  on top of the same booking core.
- **Billing:** Stripe subscriptions with a 14-day trial and webhook-driven provisioning.
- **FHIR R4 bridge:** bookings, patients and invoices exposed as standard FHIR resources.

## Architecture

- **Backend:** Python / Flask, SQLite (per-tenant isolation via a `slug` on every table)
- **Scheduling:** APScheduler for reminders, weekly reports and the recovery loop
- **Security:** Fernet (AES-128) encryption at rest for clinical fields, constant-time admin auth,
  HMAC-signed per-clinic access tokens, fail-closed encryption-key handling in production
- **Payments:** Stripe (subscriptions and webhooks)
- **Voice:** Vapi (orchestration), ElevenLabs (Greek voice), Twilio (telephony)
- **Deploy:** Railway with a persistent volume (no longer running)

## Layout

```
app.py                  Flask app, route registration, chat-booking endpoint
database.py             SQLite schema + all data access (per-tenant isolation)
back_office.py          Clinic back-office logic (finance, inventory, agents, automations)
back_office_assistant.py  Natural-language AI secretary (ask → propose → approve → execute)
crypto_util.py          Encryption at rest for clinical fields (fail-closed in production)
autonomous_loop.py      No-show / win-back recovery loop (consent-gated)
clinic_bridge.py        Internal event bus wiring booking → back-office → billing
fhir_bridge.py          FHIR R4 resource mapping
provisioning.py         New-clinic provisioning (clean, zero-fake-data)
routes/                 Flask blueprints (booking, back-office, billing, voice, FHIR, admin tools)
voice/                  Vapi/Twilio/ElevenLabs integration + AI voice assistant config
public/                 Patient/doctor-facing HTML (chat UI, back-office UI, admin tools)
tests/                  Test suite (concurrency, e2e booking flow, security, agent bridges)
omnios/                 Earlier clinic-specific agentic layer (see below)
```

## The `omnios/` folder

An earlier, clinic-specific experiment: a privacy-first agentic layer with edge anonymization of
patient data, an adversarially-verified question-answering loop that refuses to answer below a
confidence threshold, an autopilot that proposes (never silently executes) recovery and billing
actions, and a tamper-evident Merkle-anchored audit log. Details in
[`omnios/README.md`](./omnios/README.md).

The audit-log idea was later rebuilt as a general-purpose project for AI agents, with approvals,
independent witnesses and offline-verifiable evidence packs:
**[ntinosgkiou-code/omnios](https://github.com/ntinosgkiou-code/omnios)**.
