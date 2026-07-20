"""
adversarial_debate.py — STEP 4: Adversarial Multi-Agent Debate Engine.
=====================================================================
Three specialist actors argue a claim to convergence — the anti-hallucination core:
  • Proposer   — surfaces candidate facts (from the Graph-RAG KB + any asserted claims).
  • Skeptic    — independently VERIFIES each claim against the graph: direct edge, multi-hop
                 path, or unsupported; and flags contradictions (REDUCES vs CAUSES on the same pair).
  • Coordinator— scores consensus, converges (drops unsupported asserted claims across rounds),
                 and forces Human-in-the-Loop when confidence is low or a contradiction stands.

Runs on the hardened actor runtime (Step 0) and reasons over the Graph-RAG KB (Step 3). A shared
Pydantic State/Result object flows between agents. Deterministic offline (rule-based verification);
swap any agent for an LLM without changing the protocol. Tests: python core/test_adversarial_debate.py
"""
from __future__ import annotations

import asyncio
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from runtime import Actor, Supervisor
from graph_rag import GraphRAG, KnowledgeGraph, _norm_entity


# ─────────────────────────── verification primitives ───────────────────────────
_INCOMPATIBLE: dict[str, set[str]] = {
    "REDUCES": {"INCREASES", "CAUSES"},
    "INCREASES": {"REDUCES"},
    "CAUSES": {"REDUCES"},
}


def support_level(graph: KnowledgeGraph, s: str, p: str, o: str) -> tuple[str, float]:
    """direct edge → strong; any multi-hop path s..o → weak (inference); else none."""
    for pred, obj, _ in graph.neighbors(s):
        if pred == p and obj == o:
            return "direct", 0.95
    for path in graph.multi_hop(s, 3):
        if path and path[-1][1] == o:
            return "hop", 0.6
    return "none", 0.0


def find_contradiction(graph: KnowledgeGraph, s: str, p: str, o: str) -> Optional[str]:
    for pred, obj, _ in graph.neighbors(s):
        if obj == o and pred in _INCOMPATIBLE.get(p, set()):
            return pred
    return None


# ─────────────────────────── models ───────────────────────────
class Claim(BaseModel):
    model_config = ConfigDict(frozen=True)
    subject: str
    predicate: str
    object: str
    source: str = "kb"          # kb | asserted


class TranscriptTurn(BaseModel):
    model_config = ConfigDict(frozen=True)
    agent: str
    round: int
    message: str


class DebateResult(BaseModel):
    query: str
    answer: str
    confidence: float
    hitl_required: bool
    rounds: int
    supported: list[Claim]
    rejected: list[Claim]
    contradiction: bool
    transcript: list[TranscriptTurn]


# ─────────────────────────── agent handlers (closures over the KB) ───────────────────────────
def _proposer(grag: GraphRAG):
    async def handle(msg):
        query = msg.payload["query"]
        asserted = msg.payload.get("asserted", [])
        seen: dict[tuple, dict] = {}
        for ent in grag._query_entities(query):
            for pred, obj, _ in grag.graph.neighbors(ent):
                seen[(ent, pred, obj)] = {"s": ent, "p": pred, "o": obj, "source": "kb"}
        for (s, p, o) in asserted:
            key = (_norm_entity(s), p, _norm_entity(o))
            seen[key] = {"s": key[0], "p": key[1], "o": key[2], "source": "asserted"}
        return list(seen.values())
    return handle


def _skeptic(grag: GraphRAG):
    async def handle(msg):
        judged = []
        for c in msg.payload["claims"]:
            level, score = support_level(grag.graph, c["s"], c["p"], c["o"])
            contra = find_contradiction(grag.graph, c["s"], c["p"], c["o"])
            judged.append({**c, "support": level, "score": score, "contradiction": contra,
                           "verdict": "reject" if (level == "none" or contra) else "accept"})
        return judged
    return handle


def _coordinator(threshold: float):
    async def handle(msg):
        judged = msg.payload["judged"]
        supported = [c for c in judged if c["verdict"] == "accept"]
        rejected = [c for c in judged if c["verdict"] == "reject"]
        contradiction = any(c["contradiction"] for c in judged)
        conf = round(sum(c["score"] for c in supported) / len(supported), 3) if supported else 0.0
        hitl = contradiction or not supported or conf < threshold
        return {"supported": supported, "rejected": rejected, "confidence": conf,
                "hitl": hitl, "contradiction": contradiction}
    return handle


def _synthesize(supported: list[dict], query: str) -> str:
    if not supported:
        return "Δεν βρέθηκε τεκμηριωμένη απάντηση στη βάση γνώσης."
    verb = {"REDUCES": "μειώνει", "INCREASES": "αυξάνει", "CAUSES": "προκαλεί",
            "TREATS": "θεραπεύει", "CONTRAINDICATED_FOR": "αντενδείκνυται για", "IS_A": "είναι"}
    parts = [f"{c['s']} {verb.get(c['p'], c['p'])} {c['o']}" for c in supported]
    return "· ".join(parts)


# ─────────────────────────── orchestrator ───────────────────────────
async def run_debate(grag: GraphRAG, query: str, asserted_claims: Optional[list[tuple]] = None,
                     threshold: float = 0.9, max_rounds: int = 2) -> DebateResult:
    sup = Supervisor()
    proposer = sup.register(Actor("proposer", _proposer(grag)))
    skeptic = sup.register(Actor("skeptic", _skeptic(grag)))
    coordinator = sup.register(Actor("coordinator", _coordinator(threshold)))
    sup.start_all()

    transcript: list[TranscriptTurn] = []
    asserted_cur = list(asserted_claims or [])
    contradiction_ever = False
    rejected_acc: dict[tuple, dict] = {}   # audit trail of everything rejected across rounds
    verdict: dict = {}
    rnd = 0
    try:
        for rnd in range(1, max_rounds + 1):
            claims = await proposer.ask("propose", {"query": query, "asserted": asserted_cur})
            transcript.append(TranscriptTurn(agent="Proposer", round=rnd,
                                             message=f"{len(claims)} candidate claim(s) surfaced"))
            judged = await skeptic.ask("critique", {"claims": claims})
            rej = [c for c in judged if c["verdict"] == "reject"]
            for c in rej:
                rejected_acc[(c["s"], c["p"], c["o"], c["source"])] = c
            rej_desc = ", ".join(f"{c['s']}–{c['p']}–{c['o']}"
                                 + (f" (⚠ contradicts {c['contradiction']})" if c["contradiction"] else " (unsupported)")
                                 for c in rej) or "none"
            transcript.append(TranscriptTurn(agent="Skeptic", round=rnd, message=f"rejected: {rej_desc}"))
            verdict = await coordinator.ask("judge", {"judged": judged})
            contradiction_ever = contradiction_ever or verdict["contradiction"]
            transcript.append(TranscriptTurn(agent="Coordinator", round=rnd,
                                             message=f"confidence={verdict['confidence']} · hitl={verdict['hitl'] or contradiction_ever}"))
            # converge: drop only UNSUPPORTED (non-contradictory) asserted claims; keep contradictions for HITL
            drop = {(c["s"], c["p"], c["o"]) for c in rej if c["source"] == "asserted" and not c["contradiction"]}
            if not drop:
                break
            asserted_cur = [a for a in asserted_cur
                            if (_norm_entity(a[0]), a[1], _norm_entity(a[2])) not in drop]
    finally:
        await sup.shutdown()

    supported = [Claim(subject=c["s"], predicate=c["p"], object=c["o"], source=c["source"])
                 for c in verdict["supported"]]
    supported_keys = {(c["s"], c["p"], c["o"]) for c in verdict["supported"]}
    rejected = [Claim(subject=c["s"], predicate=c["p"], object=c["o"], source=c["source"])
                for (s, p, o, src), c in rejected_acc.items() if (s, p, o) not in supported_keys]
    return DebateResult(
        query=query, answer=_synthesize(verdict["supported"], query),
        confidence=verdict["confidence"], hitl_required=verdict["hitl"] or contradiction_ever,
        rounds=rnd, supported=supported, rejected=rejected,
        contradiction=contradiction_ever, transcript=transcript,
    )


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    KB = ("Η ασπιρίνη μειώνει τον πυρετό. Η ασπιρίνη αντενδείκνυται για έλκος. "
          "Το έλκος προκαλεί πόνο στο στομάχι. Η παρακεταμόλη μειώνει τον πυρετό.")

    async def demo():
        g = GraphRAG(); g.ingest_sync(KB)
        r = await run_debate(g, "ασπιρίνη πυρετό",
                             asserted_claims=[("ασπιρίνη", "CAUSES", "πυρετό")])  # a hallucination
        print("answer:", r.answer)
        print("confidence:", r.confidence, "| HITL:", r.hitl_required, "| contradiction:", r.contradiction)
        for t in r.transcript:
            print(f"  R{t.round} {t.agent}: {t.message}")
    asyncio.run(demo())
