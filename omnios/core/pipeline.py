"""
pipeline.py — INTEGRATION: Unified Medical Intelligence Layer.
=============================================================
Wires the five steps into one auditable flow, with PII kept out of the knowledge base end-to-end:

  ingest(doc):  EdgeAnonymizer(1) → SemanticChunker(2) → GraphRAG.ingest(3)
                └─ the reverse-map stays LOCAL; only redacted text ever reaches the vector store / graph.
  ask(query):   anonymize → GraphRAG hybrid retrieve(3) → AdversarialDebate verify(4) → re-identify answer
                └─ returns a grounded answer + confidence + HITL flag + sources + reasoning paths.
  act(state):   PredictiveSimulator(5) → best intervention + predicted outcome.
  bill(events): ValueBasedBilling(5) → charge a share of € actually saved.

Deterministic & offline; every component keeps its neural/LLM upgrade hook. Async, Pydantic v2.
Tests: python core/test_pipeline.py   ·   Demo: python core/pipeline.py
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

from audit import AUDIT
from edge_anonymizer import EdgeAnonymizer
from semantic_chunker import SemanticChunker
from graph_rag import GraphRAG
from adversarial_debate import run_debate
from predictive_billing import (AppointmentState, BillingResult, PredictiveSimulator,
                                 SimulationResult, ValueBasedBilling, ValueEvent)


class IngestReport(BaseModel):
    source: Optional[str]
    chunks: int
    triplets: int
    entities: int
    pii_removed: int


class AnswerReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    query: str
    answer: str
    confidence: float
    hitl_required: bool
    pii_protected: bool
    sources: list[int]
    graph_paths: list[str]
    transcript: list[str]


class MedicalIntelligencePipeline:
    def __init__(self, anonymizer: Optional[EdgeAnonymizer] = None,
                 graph_rag: Optional[GraphRAG] = None,
                 simulator: Optional[PredictiveSimulator] = None,
                 billing: Optional[ValueBasedBilling] = None,
                 debate_threshold: float = 0.95) -> None:   # YC-2026 rule: <95% confidence → HITL/freeze
        self.anonymizer = anonymizer or EdgeAnonymizer(use_ner=False)
        self.graph_rag = graph_rag or GraphRAG(chunker=SemanticChunker(max_tokens=512, min_tokens=40))
        self.simulator = simulator or PredictiveSimulator()
        self.billing = billing or ValueBasedBilling()
        self.debate_threshold = debate_threshold
        self._pii_map: dict[str, str] = {}   # token → original, LOCAL only, never sent anywhere

    # ---- ingest ----
    def ingest_sync(self, text: str, source: Optional[str] = None) -> IngestReport:
        red = self.anonymizer.anonymize_sync(text)
        self._pii_map.update(red.mapping())          # keep re-identification key local
        ing = self.graph_rag.ingest_sync(red.redacted, source=source)   # only redacted text enters the KB
        return IngestReport(source=source, chunks=ing.chunks, triplets=ing.triplets,
                            entities=ing.entities, pii_removed=red.pii_count)

    # ---- ask ----
    async def ask(self, query: str) -> AnswerReport:
        qred = self.anonymizer.anonymize_sync(query)
        search = self.graph_rag.hybrid_search(qred.redacted, k=3, hops=2)
        debate = await run_debate(self.graph_rag, qred.redacted, threshold=self.debate_threshold)
        answer = self.anonymizer.deanonymize_sync(debate.answer, self._pii_map)  # re-identify at egress
        AUDIT.record("pipeline.answer", {"q_len": len(query), "confidence": debate.confidence,
                                         "hitl": debate.hitl_required, "sources": len(search.hits)})
        return AnswerReport(
            query=query, answer=answer, confidence=debate.confidence,
            hitl_required=debate.hitl_required, pii_protected=True,
            sources=[h.chunk_index for h in search.hits], graph_paths=search.graph_paths,
            transcript=[f"{t.agent}: {t.message}" for t in debate.transcript],
        )

    # ---- act / bill ----
    def act(self, state: AppointmentState) -> SimulationResult:
        return self.simulator.simulate_sync(state)

    def bill(self, events: list[ValueEvent]) -> BillingResult:
        return self.billing.bill_sync(events)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import asyncio
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async def demo():
        pipe = MedicalIntelligencePipeline()
        kb = ("Ασθενής με ΑΜΚΑ 12036512345 και τηλέφωνο 6945123456. "
              "Η ασπιρίνη μειώνει τον πυρετό. Η ασπιρίνη αντενδείκνυται για έλκος. "
              "Το έλκος προκαλεί πόνο στο στομάχι. Η παρακεταμόλη μειώνει τον πυρετό.")
        print("① INGEST")
        rep = pipe.ingest_sync(kb, source="clinic_kb")
        print("  ", rep.model_dump())
        print("   entities in KB:", pipe.graph_rag.graph.entities(), "(no ΑΜΚΑ/phone → PII stayed out)")

        print("\n② ASK (retrieve → adversarially verify)")
        ans = await pipe.ask("τι σχέση έχει η ασπιρίνη με τον πόνο στο στομάχι;")
        print("   answer     :", ans.answer)
        print("   confidence :", ans.confidence, "| HITL:", ans.hitl_required)
        for p in ans.graph_paths:
            print("   reasoning  :", p)

        print("\n③ ACT (predict best intervention)")
        sim = pipe.act(AppointmentState(appointment_value=45, base_no_show_prob=0.30))
        print("   recommend  :", sim.recommended.value, "| next-state:", sim.predicted_next_state,
              "| €gain:", sim.expected_value_gain)

        print("\n④ BILL (charge a share of € saved)")
        b = pipe.bill([ValueEvent(kind="recovered_appointment", euros=45),
                       ValueEvent(kind="filled_slot", euros=40)])
        print("   value €", b.total_value_eur, "→ charge €", b.charge_eur, f"(ROI {b.roi}x)")

    asyncio.run(demo())
