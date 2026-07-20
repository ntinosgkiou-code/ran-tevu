"""
test_explanation.py — Explanation & Integrity Log + Confidence Gate.
Run: python core/test_explanation.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from explanation import ConfidenceGate, DecisionStatus, ExplanationLog
from audit import AUDIT


def test_confidence_gate():
    g = ConfidenceGate(floor=0.95)
    assert g.evaluate(0.96)[0] == DecisionStatus.ALLOWED
    assert g.evaluate(0.94)[0] == DecisionStatus.FROZEN, "below 95% must freeze"
    assert g.evaluate(0.99, high_risk=True)[0] == DecisionStatus.FROZEN, "high risk always freezes"
    print("✅ confidence gate: <95% or high-risk → FROZEN (manual override), else ALLOWED")


def test_explanation_carries_why_source_evidence():
    log = ExplanationLog()
    e = log.record("Answer", confidence=0.97, why=["A -[REDUCES]-> B"], sources=["kb#A"], evidence={"support": "direct"})
    assert e.status == DecisionStatus.ALLOWED and e.why and e.sources and e.evidence
    assert e.audit_seq is not None and e.integrity_ok is True
    print("✅ explanation: every decision carries why + sources + evidence + audit anchor")


def test_low_confidence_freezes_until_override():
    log = ExplanationLog()
    e = log.record("Infer diagnosis", confidence=0.72, why=["weak, multi-hop"])
    assert e.status == DecisionStatus.FROZEN and e in log.frozen()
    r = log.override(e.id, clinician_id="dr_9")
    assert r.status == DecisionStatus.ALLOWED and r.approved_by == "dr_9" and not log.frozen()
    print("✅ freeze+override: shaky decision frozen, then resolved by a clinician (audited)")


def test_high_risk_freezes():
    log = ExplanationLog()
    e = log.record("Auto-submit ΕΟΠΥΥ", confidence=0.99, high_risk=True, why=["submission"])
    assert e.status == DecisionStatus.FROZEN, "a high-risk action must freeze even at 0.99 confidence"
    print("✅ high-risk: legally-binding action frozen despite high confidence")


def test_integrity_log():
    log = ExplanationLog()
    log.record("x", 0.96, why=["y"])
    integ = log.integrity()
    assert integ["chain_intact"] is True and integ["decisions"] >= 1
    print(f"✅ integrity: {integ['decisions']} decisions logged, audit chain intact")


def main():
    test_confidence_gate()
    test_explanation_carries_why_source_evidence()
    test_low_confidence_freezes_until_override()
    test_high_risk_freezes()
    test_integrity_log()
    print("\n🎯 EXPLANATION & INTEGRITY LOG + CONFIDENCE GATE: ALL TESTS PASSED")


if __name__ == "__main__":
    main()
