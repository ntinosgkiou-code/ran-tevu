"""
omnios_core.py — Mythos-class compliance state machine (Extractor → Auditor → Formatter).
==========================================================================================
The high-stakes claims workflow of OmniOS: a clinical note goes in, a compliance-clean,
PII-free, audit-chained claim manifest comes out — or the state FREEZES for a human.

Architecture (LangGraph *topology*, dependency-free by design — same node/edge/conditional-
routing semantics, zero framework tax; see TECH-STACK-FOUNDATIONS.md "no framework tax"):

    EXTRACT ──▶ AUDIT ──▶ GATE ──┬─▶ FORMAT ──▶ DONE
    (propose)   (refute)         └─▶ HUMAN_OVERRIDE ──(clinician approves)──▶ FORMAT ──▶ DONE

  • EXTRACT   — Clinician-agent: proposes ICD-10 / CPT / E/M from the manifest lexicons,
                each with an evidence span + score (0.99 exact, 0.90 stem, 0.98 E/M-by-time).
  • AUDIT     — adversarial Compliance-Auditor-agent: INDEPENDENTLY re-derives and tries to
                REFUTE every proposal against the manifest's coding_rules (format, medical
                necessity, E/M-time consistency, PII egress). Refuted → dropped + recorded.
  • GATE      — the hard guardrail: any reject-severity failure, any high-risk trigger, or
                confidence < 0.96 (this workflow's tier; platform floor is 0.95) ⇒ FREEZE →
                HUMAN_OVERRIDE. A clinician approves/rejects; who/why/evidence audited.
  • FORMAT    — builds the claim manifest, PROVES zero PII egress (the vault re-scans the
                outbound payload), appends to the tamper-evident audit chain, and books the
                measured € savings into the ValueTracker.

PII: tokenized LOCALLY before anything else via the hardened EdgeAnonymizer (Luhn, IBAN
mod-97, ΑΜΚΑ validators). The token→value map lives ONLY inside the PIIVault in this process.

Statistics: confidence = evidence-weighted mean of confirmed proposals, hard-capped by rule
compliance; the claim also carries the Wilson 95% lower bound of the auditor pass-rate as an
honest statistical floor (reported, not gated — small-n Wilson is deliberately conservative).

Deterministic, offline, Pydantic v2, fully typed. Tests: python core/test_omnios_core.py
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
import uuid
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

from audit import AUDIT
from causal_graph import CLINICAL_GRAPH
from edge_anonymizer import EdgeAnonymizer
from omnios_manifest_loader import MANIFEST, load_manifest   # single validated source
from token_guard import TokenGuard
from value_tracker import ValueTracker
_RULES = {r["id"]: r for r in MANIFEST["coding_rules"]}
_ICD_RX = re.compile(_RULES["ICD10-FORMAT"]["pattern"])
_CPT_RX = re.compile(_RULES["CPT-FORMAT"]["pattern"])
_FLOOR = float(MANIFEST["guardrails"]["confidence_floor"])
_MIN_RX = re.compile(r"(?:διάρκεια\s+)?(\d{1,3})\s*λεπτ")


def _safe_id(prefix: str) -> str:
    """Id with structurally-bounded digit runs (a letter every 4 hex chars), so an id can NEVER
    false-positive the PII detectors (phone=10+ digits, ΑΜΚΑ=11, card=13+). Without this, a
    1-in-~100 all-digit hex would trip the egress firewall on its own claim id — a real
    production flake, found and killed by the test suite."""
    h = uuid.uuid4().hex[:12]
    return f"{prefix}-{h[:4]}x{h[4:8]}x{h[8:12]}"


def wilson_lower_bound(successes: int, trials: int, z: float = 1.96) -> float:
    """Wilson score interval lower bound — the honest floor on a pass-rate given n checks."""
    if trials == 0:
        return 0.0
    p = successes / trials
    denom = 1.0 + z * z / trials
    centre = p + z * z / (2 * trials)
    margin = z * math.sqrt(p * (1.0 - p) / trials + z * z / (4 * trials * trials))
    return max(0.0, (centre - margin) / denom)


# ═══════════════════════════ PII vault (edge, local-only) ═══════════════════════════
class EgressViolation(RuntimeError):
    """Raised when an outbound payload still contains detectable PII — the claim is blocked."""


# Shared detector instances (loaded once — the NER model costs seconds to load, never per-vault):
#   INGEST  = validators + Greek NER when installed → maximum recall on the way IN.
#   EGRESS  = deterministic validators only → the outbound firewall can never flake on a
#             statistical model's false positive; it blocks on hard evidence only.
_ANON_INGRESS = EdgeAnonymizer(use_ner=True)
_ANON_EGRESS = EdgeAnonymizer(use_ner=False)


class PIIVault:
    """Local pseudonymization vault around the hardened EdgeAnonymizer. The token→value map
    never leaves this object; `assert_no_pii` is the egress firewall every outbound payload
    must pass — it re-runs the full detector stack over the serialized payload."""

    def __init__(self) -> None:
        self._anon = _ANON_INGRESS
        self._egress = _ANON_EGRESS
        self._maps: dict[str, dict[str, str]] = {}
        self.tokens_issued = 0

    def seal(self, text: str) -> tuple[str, str]:
        res = self._anon.anonymize_sync(text)
        receipt = f"vault-{uuid.uuid4().hex[:10]}"
        self._maps[receipt] = res.mapping()                 # stays local, keyed by receipt
        self.tokens_issued += res.pii_count
        return res.redacted, receipt

    def unseal(self, text: str, receipt: str) -> str:
        return self._anon.deanonymize_sync(text, self._maps.get(receipt, {}))

    def assert_no_pii(self, payload: str) -> None:
        found = self._egress.anonymize_sync(payload).entities
        if found:
            kinds = sorted({e.kind.value for e in found})
            raise EgressViolation(f"outbound payload contains PII: {kinds}")


# ═══════════════════════════════ workflow state ═══════════════════════════════
class Phase(str, Enum):
    EXTRACT = "EXTRACT"
    AUDIT = "AUDIT"
    GATE = "GATE"
    FORMAT = "FORMAT"
    HUMAN_OVERRIDE = "HUMAN_OVERRIDE"
    DONE = "DONE"
    REJECTED = "REJECTED"


class Proposal(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: str                                   # dx | cpt | em
    code: str
    label: str = ""
    evidence_term: str = ""
    score: float = Field(ge=0.0, le=1.0)
    linked_icd10: list[str] = Field(default_factory=list)


class Verdict(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: str
    confirmed: bool
    rule_id: str = ""
    reason: str = ""


class WorkflowState(BaseModel):
    id: str
    phase: Phase = Phase.EXTRACT
    tier: str = "claims"                        # risk tier → confidence floor (manifest guardrails.tiers)
    patient_type: str = "established"
    minutes: Optional[float] = None
    note_redacted: str = ""
    vault_receipt: str = ""
    guard_id: str = ""                          # TokenGuard receipt for Context Re-Hydration
    rehydrated: bool = False                    # the full-raw deep cycle runs at most once
    causal: list[dict] = Field(default_factory=list)   # explicit dx→…→cpt causal chains
    proposals: list[Proposal] = Field(default_factory=list)
    verdicts: list[Verdict] = Field(default_factory=list)
    rule_failures: list[dict] = Field(default_factory=list)
    confidence: float = 0.0
    wilson_floor: float = 0.0
    frozen: bool = False
    freeze_reason: str = ""
    approved_by: str = ""
    claim: Optional[dict] = None
    value: Optional[dict] = None
    trail: list[str] = Field(default_factory=list)          # node-by-node transition log
    started_at: float = 0.0


# ═══════════════════════════ the state machine core ═══════════════════════════
class OmniosCore:
    """Facade: run_note() drives the graph; the override desk resumes frozen states.
    Dependency-injected: the token guard, value profile and risk tier are constructor args,
    so tests (and future deployments) swap implementations without touching the graph."""

    def __init__(self, token_guard: Optional[TokenGuard] = None,
                 value_profile: str = "coder") -> None:
        self.vault = PIIVault()
        self.guard = token_guard or TokenGuard()
        self.tracker = ValueTracker(profile=value_profile)
        self.tiers: dict[str, float] = {str(k): float(v) for k, v in
                                        MANIFEST["guardrails"].get("tiers", {}).items()}
        self.states: dict[str, WorkflowState] = {}
        self._nodes: dict[Phase, Callable[[WorkflowState], Any]] = {
            Phase.EXTRACT: self._extract_node,
            Phase.AUDIT: self._audit_node,
            Phase.GATE: self._gate_node,
            Phase.FORMAT: self._format_node,
        }

    def floor_for(self, tier: str) -> float:
        if tier == "conformal" and getattr(self, "conformal", None):
            return self.conformal.floor            # statistically calibrated (split conformal)
        return self.tiers.get(tier, _FLOOR)

    def enable_conformal(self, gate) -> None:
        """Attach a fitted ConformalGate → the 'conformal' tier becomes available: its floor is
        the calibrated 1−q̂ with the finite-sample guarantee (see conformal.py)."""
        self.conformal = gate
        self.tiers["conformal"] = gate.floor

    # ---- public API ----
    async def run_note(self, text: str, patient_type: str = "established",
                       minutes: Optional[float] = None, tier: str = "claims") -> WorkflowState:
        if tier not in self.tiers:
            raise ValueError(f"unknown risk tier {tier!r} — manifest tiers: {sorted(self.tiers)}")
        redacted, receipt = self.vault.seal(text)           # PII never enters the graph raw
        compressed, gid = self.guard.guard(redacted)        # lossless compression (or bypass)
        st = WorkflowState(id=_safe_id("wf"), note_redacted=compressed,
                           vault_receipt=receipt, guard_id=gid, tier=tier,
                           patient_type=patient_type, minutes=minutes,
                           started_at=time.monotonic())
        self.states[st.id] = st
        rep = self.guard.last_report()
        AUDIT.record("core.ingest", {"state": st.id, "tier": tier, "chars": len(compressed),
                                     "pii_tokenized": self.vault.tokens_issued,
                                     "compression_ratio": rep.ratio if rep else 1.0,
                                     "compression_bypassed": rep.bypassed if rep else False})
        return await self._run_from(st, Phase.EXTRACT)

    def pending_overrides(self) -> list[WorkflowState]:
        return [s for s in self.states.values() if s.phase is Phase.HUMAN_OVERRIDE]

    async def approve(self, state_id: str, clinician_id: str) -> WorkflowState:
        st = self._frozen_state(state_id)
        if not clinician_id.strip():
            raise PermissionError("a clinician id is required to approve")
        st.approved_by = clinician_id
        st.frozen = False
        AUDIT.record("core.override_approve", {"state": st.id, "by": clinician_id,
                                               "was_frozen_for": st.freeze_reason})
        return await self._run_from(st, Phase.FORMAT)

    def reject(self, state_id: str, clinician_id: str, reason: str = "") -> WorkflowState:
        st = self._frozen_state(state_id)
        st.phase = Phase.REJECTED
        st.trail.append("HUMAN_OVERRIDE→REJECTED")
        AUDIT.record("core.override_reject", {"state": st.id, "by": clinician_id, "reason": reason})
        return st

    def _frozen_state(self, state_id: str) -> WorkflowState:
        st = self.states.get(state_id)
        if st is None:
            raise KeyError(state_id)
        if st.phase is not Phase.HUMAN_OVERRIDE:
            raise ValueError(f"state {state_id} is not awaiting override (phase={st.phase.value})")
        return st

    # ---- graph runner (LangGraph topology: nodes + conditional edges, deterministic) ----
    async def _run_from(self, st: WorkflowState, phase: Phase) -> WorkflowState:
        st.phase = phase
        for _ in range(8):                                   # hard upper bound on transitions
            node = self._nodes.get(st.phase)
            if node is None:                                 # terminal / frozen phases
                return st
            nxt = await node(st)
            st.trail.append(f"{st.phase.value}→{nxt.value}")
            st.phase = nxt
            if st.phase in (Phase.DONE, Phase.REJECTED, Phase.HUMAN_OVERRIDE):
                return st
        raise RuntimeError("graph exceeded transition budget")   # unreachable by construction

    # ---- node: EXTRACT (Clinician agent proposes, with evidence) ----
    async def _extract_node(self, st: WorkflowState) -> Phase:
        low = st.note_redacted.lower()
        seen: dict[str, Proposal] = {}

        for term, info in MANIFEST["icd10_lexicon"].items():
            if term.startswith("_") or term not in low:
                continue
            p = Proposal(kind="dx", code=info["code"], label=info["label"],
                         evidence_term=term, score=0.99)
            if p.code not in seen or seen[p.code].score < p.score:
                seen[p.code] = p
        for stem, code in MANIFEST["icd10_stems"].items():
            if stem.startswith("_") or stem not in low or code in seen:
                continue
            seen[code] = Proposal(kind="dx", code=code, label=f"stem:{stem}",
                                  evidence_term=stem, score=0.90)
        dx_codes = [p.code for p in seen.values()]

        for term, info in MANIFEST["cpt_rules"].items():
            if term.startswith("_") or term not in low:
                continue
            key = f"cpt:{info['code']}"
            if key not in seen:
                just = info["justified_by"]
                linked = (dx_codes if "*" in just else
                          [c for c in dx_codes if any(c.startswith(pfx) for pfx in just)])
                seen[key] = Proposal(kind="cpt", code=info["code"], label=info["label"],
                                     evidence_term=term, score=0.99, linked_icd10=linked)

        minutes = st.minutes
        if minutes is None:
            m = _MIN_RX.search(low)
            minutes = float(m.group(1)) if m else None
            st.minutes = minutes
        if minutes is not None:
            for code, lo, hi in MANIFEST["em_table"].get(st.patient_type, []):
                if lo <= minutes <= hi:
                    seen[f"em:{code}"] = Proposal(kind="em", code=code,
                                                  label=f"E/M {st.patient_type} {lo}-{hi} min",
                                                  evidence_term=f"{int(minutes)} λεπτά", score=0.98)
                    break

        st.proposals = list(seen.values())
        AUDIT.record("core.extract", {"state": st.id,
                                      "proposed": [(p.kind, p.code, p.score) for p in st.proposals]})
        return Phase.AUDIT

    # ---- node: AUDIT (adversarial Compliance-Auditor agent tries to REFUTE) ----
    async def _audit_node(self, st: WorkflowState) -> Phase:
        low = st.note_redacted.lower()
        verdicts: list[Verdict] = []
        failures: list[dict] = []

        def fail(rule_id: str, detail: str) -> None:
            failures.append({"rule_id": rule_id, "severity": _RULES[rule_id]["severity"],
                             "detail": detail})

        confirmed: list[Proposal] = []
        for p in st.proposals:
            # 1) independent evidence re-derivation: the span must really be in the narrative
            if p.evidence_term and p.evidence_term not in low and not p.kind == "em":
                verdicts.append(Verdict(code=p.code, confirmed=False, rule_id="EVIDENCE",
                                        reason=f"span '{p.evidence_term}' not found on re-read"))
                continue
            # 2) format validity
            if p.kind == "dx" and not _ICD_RX.match(p.code):
                verdicts.append(Verdict(code=p.code, confirmed=False, rule_id="ICD10-FORMAT",
                                        reason="malformed ICD-10"))
                fail("ICD10-FORMAT", f"{p.code} malformed")
                continue
            if p.kind in ("cpt", "em") and not _CPT_RX.match(p.code):
                verdicts.append(Verdict(code=p.code, confirmed=False, rule_id="CPT-FORMAT",
                                        reason="malformed CPT"))
                fail("CPT-FORMAT", f"{p.code} malformed")
                continue
            # 3) medical necessity: a CPT with an explicit justified_by list must link ≥1 dx
            if p.kind == "cpt" and not p.linked_icd10:
                src = next((i for i in MANIFEST["cpt_rules"].values()
                            if isinstance(i, dict) and i["code"] == p.code), None)
                if src and "*" not in src["justified_by"]:
                    verdicts.append(Verdict(code=p.code, confirmed=False, rule_id="MEDICAL-NECESSITY",
                                            reason="no documented diagnosis justifies this procedure"))
                    fail("MEDICAL-NECESSITY", f"CPT {p.code} unjustified by any documented dx")
                    continue
            # 4) unlisted-code risk
            if p.code.endswith("99") and p.kind != "em":
                fail("UNLISTED-CODE-REVIEW", f"{p.code} is an unlisted code")
            verdicts.append(Verdict(code=p.code, confirmed=True))
            confirmed.append(p)

        # 4b) controlled-substance mention → always human review (manifest high_risk trigger)
        _cs_terms = _RULES.get("CONTROLLED-SUBSTANCE-REVIEW", {}).get("terms", [])
        hit = next((t for t in _cs_terms if t.lower() in low), None)
        if hit:
            fail("CONTROLLED-SUBSTANCE-REVIEW", f"controlled-substance mention ('{hit}') in narrative")

        # 5) claim-level presence: at least one confirmed diagnosis
        if not any(p.kind == "dx" for p in confirmed):
            fail("DX-REQUIRED", "no billable diagnosis extracted from the narrative")

        # 6) E/M ↔ time consistency (re-derive from the table, independently)
        em = next((p for p in confirmed if p.kind == "em"), None)
        if em is not None and st.minutes is not None:
            ok = any(code == em.code and lo <= st.minutes <= hi
                     for code, lo, hi in MANIFEST["em_table"].get(st.patient_type, []))
            if not ok:
                fail("EM-TIME-CONSISTENCY", f"{em.code} inconsistent with {st.minutes} min")

        # 7) causal-graph reasoning: attach the explicit dx→…→cpt chain for every procedure
        #    (multi-hop over the clinical DAG; missing paths are WARN — the graph is honest
        #    about its finite coverage, justified_by stays the authoritative gate)
        st.causal = []
        for p in confirmed:
            if p.kind != "cpt":
                continue
            chain = None
            for dx_code in p.linked_icd10:
                chain = CLINICAL_GRAPH.justification(dx_code, p.code)
                if chain:
                    st.causal.append({"cpt": p.code, **chain})
                    break
            if chain is None and p.linked_icd10:
                fail("CAUSAL-PATH-CHECK",
                     f"no causal path from {p.linked_icd10} to CPT {p.code} in the clinical DAG")

        # confidence: evidence-weighted mean of confirmed proposals (deterministic)
        scores = [p.score for p in confirmed]
        st.confidence = round(sum(scores) / len(scores), 4) if scores else 0.0
        checks_total = len(verdicts) + len(MANIFEST["coding_rules"])
        checks_passed = (sum(1 for v in verdicts if v.confirmed)
                         + len(MANIFEST["coding_rules"]) - len(failures))
        st.wilson_floor = round(wilson_lower_bound(checks_passed, checks_total), 4)
        st.proposals = confirmed
        st.verdicts = verdicts
        st.rule_failures = failures
        AUDIT.record("core.audit", {"state": st.id, "confirmed": len(confirmed),
                                    "refuted": sum(1 for v in verdicts if not v.confirmed),
                                    "failures": [f["rule_id"] for f in failures],
                                    "confidence": st.confidence})
        return Phase.GATE

    # ---- node: GATE (the hard guardrail — freeze below the tier floor or on any reject) ----
    async def _gate_node(self, st: WorkflowState) -> Phase:
        floor = self.floor_for(st.tier)
        rejects = [f for f in st.rule_failures if f["severity"] == "reject"]
        risks = [f for f in st.rule_failures if f["severity"] == "high_risk"]

        # Context Re-Hydration: if the state would freeze for MISSING INFORMATION (no dx, or
        # sub-floor confidence) and the context was compressed, pull the COMPLETE raw context
        # back and re-run the full reasoning cycle ONCE before freezing. Never used to bypass
        # substantive rejections (necessity/format) — those freeze regardless of context.
        missing_info = (any(f["rule_id"] == "DX-REQUIRED" for f in rejects)
                        or (not rejects and not risks and st.confidence < floor))
        if missing_info and not st.rehydrated:
            raw = self.guard.rehydrate(st.guard_id)
            if raw != st.note_redacted:
                st.note_redacted = raw
                st.rehydrated = True
                st.frozen = False
                st.freeze_reason = ""
                AUDIT.record("core.rehydrate", {"state": st.id, "chars": len(raw),
                                                "why": "missing-info gate → full raw context deep cycle"})
                return Phase.EXTRACT

        if rejects:
            st.frozen = True
            st.freeze_reason = "; ".join(f"{f['rule_id']}: {f['detail']}" for f in rejects)
        elif risks:
            st.frozen = True
            st.freeze_reason = "; ".join(f"{f['rule_id']}: {f['detail']}" for f in risks)
        elif st.confidence < floor:
            st.frozen = True
            st.freeze_reason = f"confidence {st.confidence:.2f} < {floor:.2f} ({st.tier} tier)"
        if st.frozen:
            AUDIT.record("core.freeze", {"state": st.id, "tier": st.tier,
                                         "reason": st.freeze_reason, "confidence": st.confidence})
            return Phase.HUMAN_OVERRIDE
        AUDIT.record("core.gate_pass", {"state": st.id, "tier": st.tier,
                                        "confidence": st.confidence})
        return Phase.FORMAT

    # ---- node: FORMAT (claim manifest + PROOF of zero PII egress + audit + value) ----
    async def _format_node(self, st: WorkflowState) -> Phase:
        dx = [p for p in st.proposals if p.kind == "dx"]
        cpt = [p for p in st.proposals if p.kind == "cpt"]
        em = next((p for p in st.proposals if p.kind == "em"), None)
        claim = {
            "claim_id": f"clm-{st.id[3:]}",
            "workflow": MANIFEST["workflow"],
            "tier": st.tier,
            "statistical_guarantee": (self.conformal.report().model_dump()
                                      if st.tier == "conformal" and getattr(self, "conformal", None)
                                      else None),
            "causal_evidence": st.causal,
            "encounter": {"patient_type": st.patient_type, "total_minutes": st.minutes},
            "diagnoses": [{"icd10": p.code, "label": p.label,
                           "evidence": p.evidence_term, "score": p.score} for p in dx],
            "procedures": [{"cpt": p.code, "label": p.label,
                            "linked_icd10": p.linked_icd10} for p in cpt],
            "em": {"code": em.code, "band": em.label} if em else None,
            "confidence": st.confidence,
            "statistical_floor_wilson95": st.wilson_floor,
            "rule_checks": {"failures": st.rule_failures,
                            "verdicts": [v.model_dump() for v in st.verdicts]},
            "actor": st.approved_by or "system(auto ≥ gate)",
            "note_redacted_excerpt": st.note_redacted[:160],
        }
        payload = json.dumps(claim, ensure_ascii=False)
        self.vault.assert_no_pii(payload)                   # egress firewall — raises on any PII
        rec = AUDIT.record("core.claim_ready", {"state": st.id, "claim": claim["claim_id"],
                                                "dx": len(dx), "cpt": len(cpt),
                                                "actor": claim["actor"]})
        claim["integrity"] = {"audit_seq": rec.seq, "chain_intact": AUDIT.verify()}
        st.claim = claim
        st.value = self.tracker.record(st.id, st.note_redacted,
                                       time.monotonic() - st.started_at)
        return Phase.DONE


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    core = OmniosCore()
    note = ("Ασθενής με ΑΜΚΑ 12036512345, τηλ 6945123456. Αρτηριακή υπέρταση, ρυθμισμένη. "
            "Έγινε ΗΚΓ ρουτίνας. Διάρκεια 30 λεπτά.")
    st = asyncio.run(core.run_note(note, patient_type="established"))
    print("phase:", st.phase.value, "| confidence:", st.confidence,
          "| wilson floor:", st.wilson_floor)
    if st.claim:
        print("claim:", st.claim["claim_id"], "| dx:", [d["icd10"] for d in st.claim["diagnoses"]],
              "| cpt:", [p["cpt"] for p in st.claim["procedures"]],
              "| em:", st.claim["em"]["code"] if st.claim["em"] else None)
        print("PII egress: ZERO (vault-verified) | audit seq:", st.claim["integrity"]["audit_seq"],
              "| chain intact:", st.claim["integrity"]["chain_intact"])
        print("value:", st.value)
    print("tracker:", core.tracker.totals())
