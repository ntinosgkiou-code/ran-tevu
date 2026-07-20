"""
explanation.py — Explanation & Integrity Log + Confidence Gate (sell EVIDENCE, not automation).
==============================================================================================
In trust-heavy healthcare, buyers pay for *evidence*, not automation. Every decision the OS makes
must show WHAT it decided, WHY, on WHAT evidence/source, at WHAT confidence, WHO approved it, and
that the record is tamper-proof. This module is that product hero:

  • ConfidenceGate — the hard guardrail: confidence < 95% OR a high-risk action ⇒ FREEZE and demand
    a human override. The agent never proceeds on a shaky or high-stakes call.
  • Explanation     — one decision's full, human-readable justification (why + sources + confidence
    + status + approver + the audit sequence that anchors it).
  • ExplanationLog  — the "Explanation & Integrity Log" screen's data source; `integrity()` re-verifies
    the tamper-evident audit chain so the whole log is provably untampered.

Deterministic, offline, Pydantic v2. Tests: python core/test_explanation.py
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from audit import AUDIT

CONFIDENCE_FLOOR = 0.95   # YC-2026 hard guardrail


class DecisionStatus(str, Enum):
    ALLOWED = "ALLOWED"     # confident + acceptable risk → may proceed (still audited)
    FROZEN = "FROZEN"       # low confidence or high risk → needs a human override


class ConfidenceGate:
    def __init__(self, floor: float = CONFIDENCE_FLOOR) -> None:
        self.floor = floor

    def evaluate(self, confidence: float, high_risk: bool = False) -> tuple[DecisionStatus, str]:
        if high_risk:
            return DecisionStatus.FROZEN, "high clinical/financial risk → manual override required"
        if confidence < self.floor:
            return DecisionStatus.FROZEN, f"confidence {confidence:.2f} < {self.floor:.2f} → manual override required"
        return DecisionStatus.ALLOWED, "confident and within risk tolerance"


class Explanation(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    action: str
    confidence: float
    status: DecisionStatus
    gate_reason: str
    why: list[str]                 # the reasoning steps / graph paths
    sources: list[str] = []        # grounded sources (KB entities, guideline refs)
    evidence: dict = {}            # structured evidence (sandbox preview, retrieved facts)
    approved_by: Optional[str] = None
    audit_seq: Optional[int] = None
    integrity_ok: bool = True


class ExplanationLog:
    """The record behind the 'Explanation & Integrity Log' screen."""

    def __init__(self, gate: Optional[ConfidenceGate] = None) -> None:
        self.gate = gate or ConfidenceGate()
        self._entries: list[Explanation] = []
        self._seq = 0

    def record(self, action: str, confidence: float, why: list[str],
               sources: Optional[list[str]] = None, evidence: Optional[dict] = None,
               high_risk: bool = False, approved_by: Optional[str] = None) -> Explanation:
        status, reason = self.gate.evaluate(confidence, high_risk)
        self._seq += 1
        rec = AUDIT.record("decision.explain", {"action": action, "confidence": round(confidence, 4),
                                                "status": status.value, "high_risk": high_risk})
        seq = rec.seq
        exp = Explanation(
            id=f"exp-{self._seq:04d}", action=action, confidence=round(confidence, 4),
            status=status, gate_reason=reason, why=why, sources=sources or [],
            evidence=evidence or {}, approved_by=approved_by, audit_seq=seq,
            integrity_ok=AUDIT.verify(),
        )
        self._entries.append(exp)
        return exp

    def override(self, explanation_id: str, clinician_id: str) -> Explanation:
        """A human resolves a FROZEN decision. Recorded for the integrity log."""
        for i, e in enumerate(self._entries):
            if e.id == explanation_id:
                if e.status != DecisionStatus.FROZEN:
                    return e
                AUDIT.record("decision.override", {"id": explanation_id, "by": clinician_id})
                resolved = e.model_copy(update={"status": DecisionStatus.ALLOWED,
                                                "approved_by": clinician_id, "integrity_ok": AUDIT.verify()})
                self._entries[i] = resolved
                return resolved
        raise KeyError(f"no explanation {explanation_id!r}")

    def entries(self) -> list[Explanation]:
        return list(self._entries)

    def frozen(self) -> list[Explanation]:
        return [e for e in self._entries if e.status == DecisionStatus.FROZEN]

    def integrity(self) -> dict:
        return {"records": len(AUDIT), "chain_intact": AUDIT.verify(),
                "decisions": len(self._entries), "frozen": len(self.frozen())}


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    log = ExplanationLog()
    a = log.record("Answer: ασπιρίνη μειώνει τον πυρετό", confidence=0.95,
                   why=["ασπιρίνη -[REDUCES]-> πυρετό (direct KB fact)"],
                   sources=["clinic_kb#aspirin"], evidence={"support": "direct"})
    b = log.record("Auto-submit ΕΟΠΥΥ execution", confidence=0.99, high_risk=True,
                   why=["monthly eΔΑΠΥ submission"], evidence={"amount": 12})
    c = log.record("Infer diagnosis from note", confidence=0.72,
                   why=["multi-hop inference, weak support"], evidence={"support": "hop"})
    for e in (a, b, c):
        print(f"[{e.status.value:8}] {e.action[:40]:40} conf={e.confidence} · {e.gate_reason}")
    print("\nfrozen (need a human):", [e.id for e in log.frozen()])
    r = log.override(c.id, clinician_id="dr_papadopoulos")
    print("after override:", r.id, "→", r.status.value, "by", r.approved_by)
    print("integrity:", log.integrity())
