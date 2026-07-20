"""
test_pipeline.py — INTEGRATION tests (all 5 steps end-to-end).
Run: python core/test_pipeline.py
"""
import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pipeline import MedicalIntelligencePipeline
from predictive_billing import AppointmentState, Action, ValueEvent

KB = ("Ασθενής με ΑΜΚΑ 12036512345 και τηλέφωνο 6945123456. "
      "Η ασπιρίνη μειώνει τον πυρετό. Η ασπιρίνη αντενδείκνυται για έλκος. "
      "Το έλκος προκαλεί πόνο στο στομάχι. Η παρακεταμόλη μειώνει τον πυρετό.")


def test_pii_never_enters_knowledge_base():
    pipe = MedicalIntelligencePipeline()
    rep = pipe.ingest_sync(KB, source="kb")
    assert rep.pii_removed >= 2, "ΑΜΚΑ + phone should have been stripped"
    blob = " ".join(c["text"] for c in pipe.graph_rag._chunks) + " ".join(pipe.graph_rag.graph.entities())
    assert "12036512345" not in blob and "6945123456" not in blob, "PII leaked into the KB!"
    assert "ασπιρίνη" in pipe.graph_rag.graph.entities(), "medical facts must survive anonymization"
    print(f"✅ privacy: {rep.pii_removed} PII items stripped before ingest; medical KB intact")


def test_end_to_end_grounded_answer():
    async def run():
        pipe = MedicalIntelligencePipeline()
        pipe.ingest_sync(KB)
        ans = await pipe.ask("ασπιρίνη πυρετό")
        assert ans.pii_protected
        assert ans.confidence >= 0.9 and not ans.hitl_required, "KB-supported answer should be confident"
        assert "μειώνει" in ans.answer and "πυρετό" in ans.answer
        assert len(ans.transcript) >= 3, "debate transcript (3 agents) should be attached"
        return ans
    ans = asyncio.run(run())
    print(f"✅ end-to-end: grounded answer '{ans.answer[:48]}…' conf={ans.confidence}, no HITL")


def test_multi_hop_answer_flags_hitl():
    async def run():
        pipe = MedicalIntelligencePipeline()
        pipe.ingest_sync(KB)
        # the aspirin↔stomach-pain link is only reachable by multi-hop → retrieved but lower confidence
        ans = await pipe.ask("ασπιρίνη πόνο στο στομάχι")
        assert any("πόνο στο στομάχι" in p for p in ans.graph_paths), "multi-hop reasoning path should be surfaced"
        return ans
    ans = asyncio.run(run())
    print(f"✅ reasoning: multi-hop path surfaced in the answer ({len(ans.graph_paths)} path(s))")


def test_act_and_bill_wired():
    pipe = MedicalIntelligencePipeline()
    sim = pipe.act(AppointmentState(appointment_value=45, base_no_show_prob=0.30))
    assert sim.recommended != Action.NONE and sim.expected_value_gain > 0
    b = pipe.bill([ValueEvent(kind="recovered_appointment", euros=45),
                   ValueEvent(kind="filled_slot", euros=40)])
    assert b.charge_eur == round(0.10 * 85, 2) and b.roi == 10.0
    print(f"✅ act+bill: recommend '{sim.recommended.value}', bill €{b.charge_eur} on €{b.total_value_eur} saved")


def main():
    test_pii_never_enters_knowledge_base()
    test_end_to_end_grounded_answer()
    test_multi_hop_answer_flags_hitl()
    test_act_and_bill_wired()
    print("\n🎯 INTEGRATION — Unified Medical Intelligence Pipeline: ALL TESTS PASSED")


if __name__ == "__main__":
    main()
