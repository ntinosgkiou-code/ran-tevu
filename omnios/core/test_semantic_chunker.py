"""
test_semantic_chunker.py — Step 2 tests (neural semantic chunking, offline lexical embedder).
Run: python core/test_semantic_chunker.py
"""
import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from semantic_chunker import SemanticChunker, split_sentences, estimate_tokens

MEDICAL = ("Ο ασθενής ανέφερε οσφυαλγία μετά από άρση βάρους. Η κλινική αξιολόγηση έδειξε μυϊκό σπασμό "
           "στην οσφυϊκή μοίρα. Το πλάνο θεραπείας περιλαμβάνει φυσικοθεραπεία και ασκήσεις ενδυνάμωσης. "
           "Ο θεραπευτής συνέστησε οκτώ συνεδρίες αποκατάστασης. ")
FINANCE = ("Το τιμολόγιο εκδόθηκε με ΦΠΑ μηδέν λόγω ιατρικής εξαίρεσης. Η πληρωμή ολοκληρώθηκε με κάρτα. "
           "Το λογιστικό υπόλοιπο του ασθενούς μηδενίστηκε. Η μηνιαία οικονομική αναφορά στάλθηκε στον ιδιοκτήτη.")


def test_topic_shift_creates_boundary():
    # overlap=0 so the topic boundary is clean (overlap is exercised in its own test)
    ch = SemanticChunker(max_tokens=400, min_tokens=15, similarity_threshold=0.30, overlap_sentences=0)
    res = ch.chunk_sync(MEDICAL + "\n\n" + FINANCE)
    assert res.total_chunks >= 2, f"topic shift should split; got {res.total_chunks}"
    med = [c.index for c in res.chunks if "φυσικοθεραπ" in c.text.lower()]
    fin = [c.index for c in res.chunks if "τιμολόγιο" in c.text.lower()]
    assert med and fin and max(med) < min(fin), "medical and finance topics must be separated in order"
    print(f"✅ semantic gating: medical→finance separated across {res.total_chunks} chunks (in order)")


def test_token_budget_respected():
    long_doc = " ".join(f"Πρόταση αριθμός {i} με κάποιο ιατρικό περιεχόμενο σχετικά με την αποκατάσταση." for i in range(40))
    ch = SemanticChunker(max_tokens=60, min_tokens=15, similarity_threshold=0.30)
    res = ch.chunk_sync(long_doc)
    assert res.total_chunks > 1, "long doc must split into multiple chunks"
    longest_sentence_tokens = max(estimate_tokens(s) for s in split_sentences(long_doc))
    for c in res.chunks:
        assert c.token_estimate <= 60 + longest_sentence_tokens, f"chunk over budget: {c.token_estimate}"
    print(f"✅ token budget: {res.total_chunks} chunks, each within max_tokens + one sentence")


def test_overlap_bridges_context():
    ch = SemanticChunker(max_tokens=45, min_tokens=10, similarity_threshold=0.30, overlap_sentences=1)
    res = ch.chunk_sync(MEDICAL)
    assert res.total_chunks >= 2
    bridged = any(res.chunks[k + 1].start_char < res.chunks[k].end_char for k in range(res.total_chunks - 1))
    assert bridged, "consecutive chunks should overlap (context bridge)"
    print("✅ overlap: consecutive chunks share a bridging sentence")


def test_coverage_and_determinism():
    doc = MEDICAL + "\n\n" + FINANCE
    ch = SemanticChunker(max_tokens=80, min_tokens=15)
    r1 = ch.chunk_sync(doc)
    r2 = ch.chunk_sync(doc)
    assert [c.text for c in r1.chunks] == [c.text for c in r2.chunks], "chunking must be deterministic"
    assert r1.chunks[0].start_char <= 2, "coverage should start at document head"
    assert r1.chunks[-1].end_char >= len(doc.strip()) - 2, "coverage should reach document tail"
    print("✅ coverage + determinism: full-doc coverage, identical output on re-run")


def test_edge_cases_and_metadata():
    ch = SemanticChunker()
    assert ch.chunk_sync("").total_chunks == 0
    assert ch.chunk_sync("   \n  ").total_chunks == 0
    res = ch.chunk_sync("Ο ασθενής έχει διάγνωση οσφυαλγίας και ξεκινά φυσικοθεραπεία.")
    assert res.chunks[0].metadata.doc_type == "clinical_note"
    assert res.chunks[0].metadata.specialty == "physio"
    print("✅ edge cases: empty→0 chunks; metadata inferred (clinical_note / physio)")


async def _async_roundtrip():
    ch = SemanticChunker(max_tokens=80, min_tokens=20)
    res = await ch.chunk(MEDICAL)
    assert res.total_chunks >= 1
    print("✅ async API: chunk() via asyncio.to_thread")


def main():
    test_topic_shift_creates_boundary()
    test_token_budget_respected()
    test_overlap_bridges_context()
    test_coverage_and_determinism()
    test_edge_cases_and_metadata()
    asyncio.run(_async_roundtrip())
    print("\n🎯 STEP 2 — Neural Semantic Chunking: ALL TESTS PASSED")


if __name__ == "__main__":
    main()
