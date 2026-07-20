"""
test_adversarial_debate.py — Step 4 tests (adversarial multi-agent debate).
Run: python core/test_adversarial_debate.py
"""
import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from graph_rag import GraphRAG
from adversarial_debate import run_debate

KB = ("Η ασπιρίνη μειώνει τον πυρετό. Η ασπιρίνη αντενδείκνυται για έλκος. "
      "Το έλκος προκαλεί πόνο στο στομάχι. Η παρακεταμόλη μειώνει τον πυρετό.")


def _grag():
    g = GraphRAG()
    g.ingest_sync(KB)
    return g


def test_supported_answer_high_confidence():
    r = asyncio.run(run_debate(_grag(), "ασπιρίνη πυρετό"))
    assert not r.hitl_required, "a fully KB-supported answer must not need HITL"
    assert r.confidence >= 0.9, r.confidence
    assert any(c.predicate == "REDUCES" and c.object == "πυρετό" for c in r.supported)
    assert len(r.transcript) >= 3, "the 3 agents must each have spoken"
    print(f"✅ supported answer: confidence={r.confidence}, no HITL, {len(r.transcript)} debate turns")


def test_hallucination_rejected_and_hitl():
    # asserting a claim that is both absent AND contradicts the KB (REDUCES vs CAUSES)
    r = asyncio.run(run_debate(_grag(), "ασπιρίνη πυρετό",
                               asserted_claims=[("ασπιρίνη", "CAUSES", "πυρετό")]))
    assert r.contradiction and r.hitl_required, "a contradicted hallucination must trip HITL"
    assert any(c.predicate == "CAUSES" and c.source == "asserted" for c in r.rejected), "hallucination must be rejected"
    assert all(not (c.predicate == "CAUSES") for c in r.supported), "hallucination must NOT enter the answer"
    print("✅ anti-hallucination: contradicted claim rejected by Skeptic → HITL, kept out of the answer")


def test_hop_inference_lowers_confidence_to_hitl():
    # true-by-inference (multi-hop) but not a direct fact → weaker support → HITL under strict threshold
    r = asyncio.run(run_debate(_grag(), "ασπιρίνη",
                               asserted_claims=[("ασπιρίνη", "CONTRAINDICATED_FOR", "πόνο στο στομάχι")],
                               threshold=0.9))
    assert not r.contradiction, "hop inference is not a contradiction"
    assert r.hitl_required and r.confidence < 0.9, f"hop-only support should lower confidence → HITL ({r.confidence})"
    assert any(c.object == "πόνο στο στομάχι" for c in r.supported), "hop-supported claim should be accepted (just low-confidence)"
    print(f"✅ inference guard: multi-hop-only claim accepted but confidence={r.confidence} → HITL")


def test_convergence_drops_unsupported():
    # an unsupported (but non-contradictory) asserted claim is dropped; the answer converges clean
    r = asyncio.run(run_debate(_grag(), "ασπιρίνη πυρετό",
                               asserted_claims=[("ασπιρίνη", "TREATS", "γρίπη")]))  # not in KB, no contradiction
    assert any(c.object == "γρίπη" for c in r.rejected), "unsupported claim should be rejected"
    assert not r.contradiction, "no contradiction here"
    assert r.rounds >= 1
    print("✅ convergence: unsupported non-contradictory claim dropped, answer stays grounded")


def main():
    test_supported_answer_high_confidence()
    test_hallucination_rejected_and_hitl()
    test_hop_inference_lowers_confidence_to_hitl()
    test_convergence_drops_unsupported()
    print("\n🎯 STEP 4 — Adversarial Multi-Agent Debate: ALL TESTS PASSED")


if __name__ == "__main__":
    main()
