"""
test_graph_rag.py — Step 3 tests (hybrid knowledge-graph + vector RAG, offline).
Run: python core/test_graph_rag.py
"""
import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from graph_rag import GraphRAG, TripletExtractor

KB = ("Η ασπιρίνη μειώνει τον πυρετό. Η ασπιρίνη αντενδείκνυται για έλκος. "
      "Το έλκος προκαλεί πόνο στο στομάχι. Η παρακεταμόλη μειώνει τον πυρετό.")


def test_triplet_extraction():
    trips = TripletExtractor().extract(KB)
    got = {(t.subject, t.predicate, t.object) for t in trips}
    assert ("ασπιρίνη", "REDUCES", "πυρετό") in got, got
    assert ("ασπιρίνη", "CONTRAINDICATED_FOR", "έλκος") in got, got
    assert ("έλκος", "CAUSES", "πόνο στο στομάχι") in got, got
    print(f"✅ triplet extraction: {len(got)} S-P-O relations parsed (CAUSES/REDUCES/CONTRAINDICATED)")


def test_vector_recall():
    g = GraphRAG()
    g.ingest_sync(KB)
    hits = g.vector_search("τι μειώνει τον πυρετό;", k=2)
    assert hits and "μειών" in hits[0][1]["text"].lower(), "vector search should recall the 'reduces fever' passage"
    print("✅ vector recall: nearest passage retrieved by cosine")


def test_multi_hop_reasoning():
    g = GraphRAG()
    g.ingest_sync(KB)
    # aspirin —CONTRAINDICATED→ ulcer —CAUSES→ stomach pain  (a link no single sentence states)
    paths = g.graph.multi_hop("ασπιρίνη", depth=2)
    reached = [p for p in paths if p and p[-1][1] == "πόνο στο στομάχι"]
    assert reached, f"2-hop aspirin→ulcer→pain path should exist; paths={paths}"
    assert any(step[0] == "CONTRAINDICATED_FOR" for step in reached[0]), "path must traverse the contraindication edge"
    print("✅ multi-hop: aspirin →CONTRAINDICATED→ ulcer →CAUSES→ stomach-pain reasoned across 2 hops")


def test_hybrid_search_fuses_vector_and_graph():
    g = GraphRAG()
    g.ingest_sync(KB)
    r = g.hybrid_search("ασπιρίνη και πόνο", k=3, hops=2)
    assert r.hits, "hybrid search returned no hits"
    assert any("πόνο στο στομάχι" in p for p in r.graph_paths), f"graph path to pain missing: {r.graph_paths}"
    assert any(h.via in ("hybrid", "graph") for h in r.hits), "graph signal should influence ranking"
    print(f"✅ hybrid search: {len(r.hits)} ranked hits + {len(r.graph_paths)} reasoning paths (vector⊕graph)")


def test_ingest_counts_and_async():
    async def run():
        g = GraphRAG()
        ing = await g.ingest(KB)
        assert ing.triplets >= 3 and ing.entities >= 4, ing
        r = await g.search("ασπιρίνη", k=2, hops=2)
        assert r.hits
        return ing
    ing = asyncio.run(run())
    print(f"✅ async ingest/search: {ing.triplets} triplets, {ing.entities} entities, {ing.chunks} chunk(s)")


def main():
    test_triplet_extraction()
    test_vector_recall()
    test_multi_hop_reasoning()
    test_hybrid_search_fuses_vector_and_graph()
    test_ingest_counts_and_async()
    print("\n🎯 STEP 3 — Hybrid Graph RAG: ALL TESTS PASSED")


if __name__ == "__main__":
    main()
