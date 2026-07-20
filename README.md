# Ran-Tevu + OmniOS

Two products, one operator: a shipped, revenue-facing SaaS (**Ran-Tevu**) and the deeper agentic
research platform it's growing into (**OmniOS**, see [`omnios/`](./omnios)).

---

# Ran-Tevu

AI booking and back-office platform for Greek doctors and clinics — live at **[ran-tevu.gr](https://www.ran-tevu.gr)**.

Patients book through an AI chat instead of calling. The clinic gets a secretary-style back-office —
patient files, invoicing with Greek VAT rules, no-show recovery, and a revenue dashboard — with
per-clinic Stripe billing and an optional AI phone receptionist.

This repository is a source snapshot for review — secrets, patient data, and internal business
documents are intentionally excluded (see `.gitignore` / below).

## What it does

- **Patient-facing booking** — an AI chat (Anthropic Claude) understands natural-language requests,
  checks real availability, and books the appointment. No phone call required.
- **Back-office for the clinic** — day view with check-in, patient folders with encrypted clinical
  notes, invoicing (auto Greek VAT / medical exemption), payments, inventory, and a "who owes me"
  ledger.
- **No-show recovery** — an autonomous loop detects no-shows and dormant patients and proposes
  win-back outreach; every action is consent-gated and audit-logged.
- **Multi-channel** — SMS (Twilio), email (Resend), and an AI voice receptionist (Vapi + ElevenLabs)
  layer on top of the same booking core.
- **Billing** — Stripe subscriptions with a 14-day trial, webhook-driven provisioning.
- **FHIR R4 bridge** — bookings/patients/invoices are exposed as standard FHIR resources for
  interoperability with other clinical systems.

## Architecture

- **Backend**: Python / Flask, SQLite (per-tenant isolation via a `slug` on every table)
- **Scheduling**: APScheduler for reminders, weekly reports, and the autonomous recovery loop
- **Security**: Fernet (AES-128) encryption at rest for clinical fields, constant-time admin auth,
  HMAC-signed per-clinic access tokens, fail-closed encryption key handling in production
- **Payments**: Stripe (subscriptions + webhooks)
- **Voice**: Vapi (orchestration) + ElevenLabs (Greek voice) + Twilio (telephony)
- **Deploy**: Railway, with a persistent volume for the database

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
```

## Status

Live in production, onboarding real Greek clinics. Built and directed by a solo, AI-native
operator — architecture, security review, and shipping decisions are mine; implementation is
done through AI-assisted engineering.

---

# OmniOS

The next layer up — see **[`omnios/README.md`](./omnios/README.md)** for the full write-up.
Short version: a privacy-first, verifiable agentic operating layer for clinics — an autopilot that
proposes (never silently executes) no-show recovery and billing actions, a clinical Q&A engine
that refuses to answer below a confidence threshold rather than guess, and a tamper-evident,
Merkle-anchored audit log any clinic can publish externally to prove nothing was altered. Deployed
as a live, password-gated, multi-tenant pilot.
