"""
graph_rag.py — STEP 3: Hybrid Knowledge-Graph + Vector RAG.
==========================================================
Plain vector RAG loses relational context; a knowledge graph loses fuzzy recall. This fuses both:
  • VECTOR recall — cosine over chunk embeddings finds semantically-near passages.
  • GRAPH reasoning — Subject→Predicate→Object triplets enable MULTI-HOP traversal
    (e.g. aspirin —CONTRAINDICATED→ ulcer —CAUSES→ stomach pain), the hidden links a vector
    store can't reach.
  • HYBRID search — vector hits, boosted by graph-connected chunks, plus the multi-hop paths.

Offline & deterministic: dict-vector cosine (reuses Step-2 embedder) + rule-based triplet
extraction + a pure-Python directed multigraph — no numpy / networkx / LLM required. The
embedder and the triplet extractor are both pluggable (neural / LLM) without touching the engine.
Async, Pydantic v2, typed. Tests: python core/test_graph_rag.py
"""
from __future__ import annotations

import asyncio
import re
from typing import Iterable, Optional

from pydantic import BaseModel, ConfigDict, Field

from semantic_chunker import Embedder, LexicalEmbedder, SemanticChunker, split_sentences


# ─────────────────────────── entity normalization ───────────────────────────
_ARTICLES = {"ο", "η", "το", "οι", "τα", "τον", "την", "του", "της", "των", "a", "an", "the"}


def _norm_entity(s: str) -> str:
    s = s.strip().strip(".,·;:!?()[]\"'").strip()
    toks = s.split()
    while toks and toks[0].lower() in _ARTICLES:
        toks.pop(0)
    return " ".join(toks).lower()


def _cut_object(s: str) -> str:
    # keep the first clause only (stop at a conjunction / comma)
    s = re.split(r"\s+(?:και|ή|,|;|·)\s+", s, maxsplit=1)[0]
    return _norm_entity(s)


# ─────────────────────────── triplet extraction ───────────────────────────
# Specific relations first; the broad IS_A ("είναι"/"is") is LAST so it never steals a
# more-specific trigger. Each is (regex-trigger, canonical predicate).
_TRIGGERS: list[tuple[str, str]] = [
    (r"αντενδείκνυ(?:ται|νται)", "CONTRAINDICATED_FOR"),
    (r"προκαλ(?:εί|ούν)", "CAUSES"),
    (r"μειών(?:ει|ουν)", "REDUCES"),
    (r"αυξάν(?:ει|ουν)", "INCREASES"),
    (r"θεραπεύ(?:ει|ουν)", "TREATS"),
    (r"contraindicated", "CONTRAINDICATED_FOR"),
    (r"causes?", "CAUSES"),
    (r"reduces?|lowers?", "REDUCES"),
    (r"increases?", "INCREASES"),
    (r"treats?", "TREATS"),
    (r"αποτελ(?:εί|ούν)|είναι", "IS_A"),
    (r"is(?:\s+an?|\s+the)?", "IS_A"),
]
_PREP = r"(?:για|σε|στον|στη[νσ]?|στο|to|for|with)"


class Triplet(BaseModel):
    model_config = ConfigDict(frozen=True)
    subject: str
    predicate: str
    object: str
    chunk: int = -1
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class TripletExtractor:
    """Rule-based S-P-O extraction (deterministic, offline). Swap in an LLM extractor by
    passing a callable(text)->list[Triplet] to GraphRAG; the graph/search stay identical."""

    def extract(self, text: str, chunk: int = -1) -> list[Triplet]:
        out: list[Triplet] = []
        for sent in split_sentences(text):
            for trig, pred in _TRIGGERS:
                m = re.search(r"(?i)^(?P<s>.+?)\s+(?:" + trig + r")\s+" + _PREP + r"?\s*(?P<o>.+)$", sent)
                if not m:
                    continue
                subj, obj = _norm_entity(m.group("s")), _cut_object(m.group("o"))
                if subj and obj and subj != obj:
                    out.append(Triplet(subject=subj, predicate=pred, object=obj, chunk=chunk))
                break  # first (most specific) trigger wins per sentence
        return out


# ─────────────────────────── pure-python knowledge graph ───────────────────────────
class KnowledgeGraph:
    def __init__(self) -> None:
        self._adj: dict[str, list[tuple[str, str, int]]] = {}   # subject -> [(predicate, object, chunk)]

    def add(self, s: str, p: str, o: str, chunk: int) -> None:
        self._adj.setdefault(s, []).append((p, o, chunk))
        self._adj.setdefault(o, [])   # objects are nodes too (so they can be traversed from)

    def entities(self) -> list[str]:
        return [e for e in self._adj if e]

    def neighbors(self, entity: str) -> list[tuple[str, str, int]]:
        return self._adj.get(entity, [])

    def multi_hop(self, start: str, depth: int) -> list[list[tuple[str, str]]]:
        """All (predicate, object) paths up to `depth` hops from `start` (cycle-safe)."""
        paths: list[list[tuple[str, str]]] = []

        def dfs(node: str, path: list[tuple[str, str]], seen: set[str], d: int) -> None:
            if d == 0:
                return
            for p, o, _ in self._adj.get(node, []):
                if o in seen:
                    continue
                step = path + [(p, o)]
                paths.append(step)
                dfs(o, step, seen | {o}, d - 1)

        dfs(start, [], {start}, depth)
        return paths


# ─────────────────────────── result models ───────────────────────────
class IngestResult(BaseModel):
    chunks: int
    triplets: int
    entities: int


class SearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)
    chunk_index: int
    text: str
    score: float
    via: str   # vector | graph | hybrid


class SearchResult(BaseModel):
    query: str
    hits: list[SearchHit]
    graph_paths: list[str]


# ─────────────────────────── the engine ───────────────────────────
class GraphRAG:
    def __init__(self, embedder: Optional[Embedder] = None,
                 extractor: Optional[TripletExtractor] = None,
                 chunker: Optional[SemanticChunker] = None,
                 graph_boost: float = 0.25) -> None:
        self.embedder = embedder or LexicalEmbedder()
        self.extractor = extractor or TripletExtractor()
        self.chunker = chunker or SemanticChunker(max_tokens=512, min_tokens=40)
        self.graph_boost = graph_boost
        self.graph = KnowledgeGraph()
        self._chunks: list[dict] = []                 # {index, text, vec}
        self._entity_chunks: dict[str, set[int]] = {}
        self._triplet_count = 0

    # ---- ingest ----
    async def ingest(self, text: str, source: Optional[str] = None) -> IngestResult:
        return await asyncio.to_thread(self.ingest_sync, text, source)

    def ingest_sync(self, text: str, source: Optional[str] = None) -> IngestResult:
        res = self.chunker.chunk_sync(text)
        for c in res.chunks:
            idx = len(self._chunks)
            self._chunks.append({"index": idx, "text": c.text, "vec": self.embedder.embed([c.text])[0]})
            for t in self.extractor.extract(c.text, chunk=idx):
                self.graph.add(t.subject, t.predicate, t.object, idx)
                self._entity_chunks.setdefault(t.subject, set()).add(idx)
                self._entity_chunks.setdefault(t.object, set()).add(idx)
                self._triplet_count += 1
        return IngestResult(chunks=len(res.chunks), triplets=self._triplet_count,
                            entities=len(self.graph.entities()))

    # ---- persistence (system of record — KB survives a restart) ----
    def snapshot(self) -> dict:
        triplets = [[s, p, o, ch] for s, edges in self.graph._adj.items() for (p, o, ch) in edges]
        return {"chunks": [c["text"] for c in self._chunks], "triplets": triplets}

    def restore(self, snap: dict) -> "GraphRAG":
        self._chunks, self.graph, self._entity_chunks, self._triplet_count = [], KnowledgeGraph(), {}, 0
        for i, text in enumerate(snap.get("chunks", [])):
            self._chunks.append({"index": i, "text": text, "vec": self.embedder.embed([text])[0]})
        for s, p, o, ch in snap.get("triplets", []):
            self.graph.add(s, p, o, ch)
            self._entity_chunks.setdefault(s, set()).add(ch)
            self._entity_chunks.setdefault(o, set()).add(ch)
            self._triplet_count += 1
        return self

    # ---- retrieval ----
    def vector_search(self, query: str, k: int = 5) -> list[tuple[float, dict]]:
        qv = self.embedder.embed([query])[0]
        scored = [(self.embedder.similarity(qv, c["vec"]), c) for c in self._chunks]
        scored.sort(key=lambda x: -x[0])
        return scored[:k]

    def _query_entities(self, query: str) -> list[str]:
        q = query.lower()
        # longest entities first so "πόνο στο στομάχι" beats "πόνο"
        return [e for e in sorted(self.graph.entities(), key=len, reverse=True) if e and e in q]

    async def search(self, query: str, k: int = 3, hops: int = 2) -> SearchResult:
        return await asyncio.to_thread(self.hybrid_search, query, k, hops)

    def hybrid_search(self, query: str, k: int = 3, hops: int = 2) -> SearchResult:
        combined: dict[int, dict] = {}
        for score, c in self.vector_search(query, k=len(self._chunks) or 1):
            combined[c["index"]] = {"text": c["text"], "score": round(score, 4), "via": "vector"}

        paths: list[str] = []
        for ent in self._query_entities(query):
            for idx in self._entity_chunks.get(ent, set()):
                if idx in combined:
                    combined[idx]["score"] = round(combined[idx]["score"] + self.graph_boost, 4)
                    combined[idx]["via"] = "hybrid"
                else:
                    combined[idx] = {"text": self._chunks[idx]["text"],
                                     "score": self.graph_boost, "via": "graph"}
            for path in self.graph.multi_hop(ent, hops):
                readable = ent + "".join(f" -[{p}]-> {o}" for p, o in path)
                paths.append(readable)

        hits = [SearchHit(chunk_index=i, text=v["text"], score=v["score"], via=v["via"])
                for i, v in sorted(combined.items(), key=lambda kv: -kv[1]["score"])][:k]
        # dedupe paths, keep the longest (most informative) prefix-unique ones
        seen: set[str] = set()
        uniq: list[str] = []
        for p in sorted(paths, key=len, reverse=True):
            if not any(p in s for s in seen):
                uniq.append(p)
                seen.add(p)
        return SearchResult(query=query, hits=hits, graph_paths=uniq)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    KB = ("Η ασπιρίνη μειώνει τον πυρετό. Η ασπιρίνη αντενδείκνυται για έλκος. "
          "Το έλκος προκαλεί πόνο στο στομάχι. Η παρακεταμόλη μειώνει τον πυρετό.")
    g = GraphRAG()
    print("ingest:", g.ingest_sync(KB).model_dump())
    r = g.hybrid_search("τι σχέση έχει η ασπιρίνη με τον πόνο;", k=3, hops=2)
    print("top hit:", r.hits[0].text[:60], "| via:", r.hits[0].via)
    for p in r.graph_paths:
        print("  path:", p)
