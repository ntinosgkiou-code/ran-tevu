# OmniOS

A privacy-first, verifiable agentic operating layer for clinics. *Earlier experiment, offline since
October 2026; the general-purpose successor is
[ntinosgkiou-code/omnios](https://github.com/ntinosgkiou-code/omnios).*

Documents are ingested with patient data stripped **at the edge**; questions are answered by an
adversarially-verified retrieval loop that **rejects unsupported claims and escalates to a human
when unsure** — no "0% hallucination" theater. An autopilot proposes no-show recovery and billing
actions for a clinician to approve; every decision is written to a tamper-evident, Merkle-anchored
audit log any clinic can independently verify.

> **On language:** this README is in English, and code (function/variable names, architecture) is
> English throughout. Clinical note content, some code comments, and the gate/UI copy are Greek —
> the intended target market.

## Architecture

```
Document ─► [1] EdgeAnonymizer ─► [2] SemanticChunker ─► [3] GraphRAG ──────────┐
              PII → tokens          embedding-gated        vectors + S·P·O       │
              (local reverse-map)   semantic chunks        triplets + multi-hop  │
                                                                                  │
Question ────────────────────────────────────────────────────────────────────►  │
                                                                                  ▼
                                        [4] Adversarial Debate  ─► grounded answer
                                        Proposer · Skeptic · Coordinator   + confidence + HITL
                                                                                  │
Appointment ─► [5] Predictive Simulator ─► best intervention      Billing ─► % of € saved
                   (MDP, evidence-anchored)   + next-state dist.       (ROI made explicit)
                                                                                  ▼
                              pipeline.py  ─►  api.py  (FastAPI: /ingest /ask /act /bill /clinic/*)
```

## Components (all in `core/`, async · Pydantic v2 · typed · tested)

| Module | Role |
|---|---|
| `edge_anonymizer.py` | Local PII pseudonymization — AMKA, IBAN (mod-97), card (Luhn), email, phone, name; reversible tokens; reverse-map never leaves the process |
| `semantic_chunker.py` | Embedding distance-gated chunking; char-n-gram embedder for Greek morphology |
| `graph_rag.py` | Vector recall ⊕ knowledge-graph multi-hop reasoning (pure-Python, no deps) |
| `adversarial_debate.py` | Proposer · Skeptic · Coordinator — verifies support, flags contradictions, forces human-in-the-loop below confidence threshold |
| `predictive_billing.py` | MDP intervention model (evidence-anchored no-show reduction) + value-based billing |
| `clinic_store.py` / `clinic_autopilot.py` | The clinic system of record + the governed autopilot that proposes (never silently executes) recovery/billing actions |
| `audit.py` / `merkle.py` | HMAC hash-chain audit log + a Merkle root any clinic can publish externally to prove nothing was altered (RFC 6962 pattern) |
| `governance.py` | Action governor — permission → sandbox preview → approval → execute → audit, on every acting endpoint |
| `omnios_core.py` | Clinical-claims pipeline — extracts ICD-10/CPT codes and E/M level from a note, gated by confidence and controlled-substance/DX-required safety rules |
| `fhir_bridge.py` | FHIR R4 resource mapping (Patient/Appointment/Invoice) |
| `auth.py` | Per-tenant API-key store + scopes; every acting endpoint is secured, health stays public |
| `api.py` | FastAPI service — ties it all together, plus the private-demo password gate |

## Design honesty

- **Deterministic offline core, pluggable neural/LLM.** Every embedder/extractor has a
  zero-external-dependency fallback and a hook for a real model.
- **Not zero-knowledge.** An agent must read text to reason; PII-tokenization + local reverse-map +
  minimized payloads instead — stated plainly, not hidden.
- **GDPR-first** (Greek clinics); a HIPAA layer is a config switch, not hardcoded.
- **Clinical documentation stays human-signed** (SaMD/liability boundary) — this layer informs and
  verifies, it does not auto-diagnose.
- **Multi-tenant, isolated** — each clinic's API key is pinned to its own tenant; verified live that
  switching tenant context returns that tenant's own data, not another clinic's.
- **Audit is real and growing** — the hash-chain is checked for tamper-evidence on every `/health`
  call and genuinely accumulates a record per governed action (verified live, not a static counter).

## Run it

```bash
cd mit-demo
python core/pipeline.py            # end-to-end demo (ingest → answer → act → bill)
uvicorn core.api:app --port 8010   # the microservice
python core/test_*.py              # each module has its own suite
```

## Status

Deployed and running a live pilot (password-gated, multi-tenant). Built and directed by a solo,
AI-native operator — architecture, security review, and shipping decisions are mine;
implementation is done through AI-assisted engineering.
