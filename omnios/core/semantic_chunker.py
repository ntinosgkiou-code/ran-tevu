"""
semantic_chunker.py — STEP 2: Neural Semantic Chunking.
=======================================================
Splits large documents into semantically-coherent chunks via **embedding distance gating**:
a sentence joins the current chunk while it stays cohesive with the chunk centroid AND within
the token budget; a topic shift (cohesion drop) OR the budget opens a new chunk, carrying an
overlap of trailing sentences so meaning is never cut mid-thought and context bridges chunks.

Offline & deterministic by default (a lexical TF embedder gives real cohesion with zero deps);
plug a neural embedder (sentence-transformers) for true semantic gating. Async, Pydantic v2,
fully typed, production-ready. Tests: python core/test_semantic_chunker.py
"""
from __future__ import annotations

import asyncio
import math
import re
from collections import Counter
from typing import Optional, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


# ─────────────────────────── token + sentence utilities ───────────────────────────
def estimate_tokens(text: str) -> int:
    """Cheap, model-agnostic token estimate (~4 chars/token, floored by word count)."""
    return max(len(text) // 4, len(text.split()))


_ABBREV = {"κ", "κα", "κλπ", "π.χ", "δρ", "τηλ", "αρ", "dr", "mr", "mrs", "vs", "e.g", "i.e", "no"}


def _ends_with_abbrev(fragment: str) -> bool:
    tail = fragment.rstrip().split()
    return bool(tail) and tail[-1].rstrip(".").lower() in _ABBREV


def split_sentences(text: str) -> list[str]:
    """Greek + English sentence splitter. Splits on . ! ? ; · followed by whitespace, keeps
    paragraph boundaries, and re-merges obvious abbreviation false-splits."""
    text = text.strip()
    if not text:
        return []
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text):          # paragraphs are hard boundaries
        block = block.strip()
        if not block:
            continue
        raw = re.split(r"(?<=[.!?;·])\s+", block)
        for frag in raw:
            frag = frag.strip()
            if not frag:
                continue
            if out and _ends_with_abbrev(out[-1]):
                out[-1] = out[-1] + " " + frag
            else:
                out.append(frag)
    return out


# ─────────────────────────── embedder protocol + offline default ───────────────────────────
@runtime_checkable
class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list: ...
    def similarity(self, a, b) -> float: ...
    def centroid(self, vecs: Sequence) -> object: ...


class LexicalEmbedder:
    """Dependency-free TF embedder. Vectors are L2-normalized term→weight dicts; cosine = dot.
    Captures lexical cohesion (same-topic sentences share terms) — enough for real gating offline."""

    _TOKEN = re.compile(r"[A-Za-zΑ-Ωα-ωΆΈΉΊΌΎΏάέήίόύώϊϋΐΰ0-9]+")

    @staticmethod
    def _char_ngrams(text: str, n: int = 4) -> list[str]:
        s = re.sub(r"\s+", " ", text).strip()
        return [s[i:i + n] for i in range(len(s) - n + 1)] if len(s) >= n else ([s] if s else [])

    def _vec(self, text: str) -> dict[str, float]:
        low = text.lower()
        words = self._TOKEN.findall(low)
        if not words:
            return {}
        # Hybrid: word tokens + character 4-grams. The n-grams capture Greek morphology
        # (θεραπ-εία / θεραπ-ευτής / φυσικο-θεραπ-εία share stems) so same-topic sentences
        # score high while cross-topic stay low — real cohesion without stemming or ML.
        c: Counter = Counter()
        for w in words:
            c["w:" + w] += 1.0
        for g in self._char_ngrams(low, 4):
            c["c:" + g] += 0.5
        norm = math.sqrt(sum(v * v for v in c.values())) or 1.0
        return {k: v / norm for k, v in c.items()}

    def embed(self, texts: Sequence[str]) -> list[dict[str, float]]:
        return [self._vec(t) for t in texts]

    def similarity(self, a: dict, b: dict) -> float:
        if not a or not b:
            return 0.0
        small, big = (a, b) if len(a) <= len(b) else (b, a)
        return sum(w * big.get(k, 0.0) for k, w in small.items())

    def centroid(self, vecs: Sequence[dict]) -> dict[str, float]:
        acc: Counter = Counter()
        for v in vecs:
            for k, val in v.items():
                acc[k] += val
        n = len(vecs) or 1
        raw = {k: val / n for k, val in acc.items()}
        norm = math.sqrt(sum(v * v for v in raw.values())) or 1.0
        return {k: v / norm for k, v in raw.items()}


def default_embedder() -> Embedder:
    """Neural embedder if sentence-transformers is installed, else the offline lexical one."""
    try:
        from sentence_transformers import SentenceTransformer  # optional heavy dep
        import numpy as np

        class _NeuralEmbedder:
            def __init__(self) -> None:
                self._m = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")

            def embed(self, texts):
                return list(self._m.encode(list(texts), normalize_embeddings=True))

            def similarity(self, a, b):
                return float(np.dot(a, b))

            def centroid(self, vecs):
                m = np.mean(np.stack(vecs), axis=0)
                n = np.linalg.norm(m) or 1.0
                return m / n

        return _NeuralEmbedder()
    except Exception:
        return LexicalEmbedder()


# ─────────────────────────── models ───────────────────────────
class ChunkMetadata(BaseModel):
    source: Optional[str] = None
    doc_type: Optional[str] = None       # e.g. clinical_note | research | fhir | invoice | generic
    specialty: Optional[str] = None
    extra: dict = Field(default_factory=dict)


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)
    index: int
    text: str
    token_estimate: int
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    sentence_count: int
    metadata: ChunkMetadata = Field(default_factory=ChunkMetadata)


class ChunkingResult(BaseModel):
    doc_id: Optional[str] = None
    chunks: list[Chunk]

    @property
    def total_chunks(self) -> int:
        return len(self.chunks)


# ─────────────────────────── metadata inference (best-effort) ───────────────────────────
_SPECIALTY_CUES = {
    "physio": ("φυσικοθερ", "αποκατάσταση", "μυοσκελετ", "physiother"),
    "dental": ("οδοντ", "σφράγισμα", "dental", "teeth"),
    "dermatology": ("δερματ", "σπίλο", "derm", "skin"),
    "cardiology": ("καρδι", "cardio", "ecg", "ηκγ"),
}


def infer_metadata(text: str, source: Optional[str] = None) -> ChunkMetadata:
    low = text.lower()
    if low.strip().startswith("{") and '"resourcetype"' in low.replace(" ", ""):
        doc_type = "fhir"
    elif any(k in low for k in ("abstract", "βιβλιογραφία", "references", "doi:")):
        doc_type = "research"
    elif any(k in low for k in ("τιμολόγιο", "invoice", "αμκα", "φπα")):
        doc_type = "invoice"
    elif any(k in low for k in ("ασθεν", "διάγνωση", "θεραπεία", "συνεδρία", "patient")):
        doc_type = "clinical_note"
    else:
        doc_type = "generic"
    specialty = next((name for name, cues in _SPECIALTY_CUES.items() if any(c in low for c in cues)), None)
    return ChunkMetadata(source=source, doc_type=doc_type, specialty=specialty)


# ─────────────────────────── the chunker ───────────────────────────
class SemanticChunker:
    def __init__(
        self,
        max_tokens: int = 512,
        min_tokens: int = 120,
        overlap_sentences: int = 1,
        similarity_threshold: float = 0.30,
        embedder: Optional[Embedder] = None,
    ) -> None:
        if min_tokens > max_tokens:
            raise ValueError("min_tokens must be <= max_tokens")
        self.max_tokens = max_tokens
        self.min_tokens = min_tokens
        self.overlap_sentences = max(0, overlap_sentences)
        self.similarity_threshold = similarity_threshold
        self.embedder = embedder or default_embedder()

    async def chunk(self, text: str, metadata: Optional[ChunkMetadata] = None,
                    doc_id: Optional[str] = None) -> ChunkingResult:
        return await asyncio.to_thread(self.chunk_sync, text, metadata, doc_id)

    def chunk_sync(self, text: str, metadata: Optional[ChunkMetadata] = None,
                   doc_id: Optional[str] = None) -> ChunkingResult:
        if not isinstance(text, str):
            raise TypeError(f"text must be str, got {type(text).__name__}")
        meta = metadata or infer_metadata(text)
        sents = split_sentences(text)
        if not sents:
            return ChunkingResult(doc_id=doc_id, chunks=[])

        vecs = self.embedder.embed(sents)
        spans = self._locate_spans(text, sents)

        chunks: list[Chunk] = []
        cur: list[int] = []
        cur_tokens = 0
        for i in range(len(sents)):
            stoks = estimate_tokens(sents[i])
            if not cur:
                cur, cur_tokens = [i], stoks
                continue
            centroid = self.embedder.centroid([vecs[j] for j in cur])
            cohesion = self.embedder.similarity(vecs[i], centroid)
            over_budget = cur_tokens + stoks > self.max_tokens
            topic_shift = cohesion < self.similarity_threshold and cur_tokens >= self.min_tokens
            if over_budget or topic_shift:
                chunks.append(self._emit(text, sents, spans, cur, len(chunks), meta))
                overlap = cur[-self.overlap_sentences:] if self.overlap_sentences else []
                cur = overlap + [i]
                cur_tokens = sum(estimate_tokens(sents[j]) for j in cur)
            else:
                cur.append(i)
                cur_tokens += stoks
        if cur:
            chunks.append(self._emit(text, sents, spans, cur, len(chunks), meta))
        return ChunkingResult(doc_id=doc_id, chunks=chunks)

    @staticmethod
    def _locate_spans(text: str, sents: Sequence[str]) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        cur = 0
        for s in sents:
            idx = text.find(s, cur)
            if idx == -1:
                idx = cur
            spans.append((idx, idx + len(s)))
            cur = idx + len(s)
        return spans

    @staticmethod
    def _emit(text: str, sents, spans, idxs: list[int], index: int, meta: ChunkMetadata) -> Chunk:
        start = spans[idxs[0]][0]
        end = spans[idxs[-1]][1]
        body = text[start:end]
        return Chunk(index=index, text=body, token_estimate=estimate_tokens(body),
                     start_char=start, end_char=end, sentence_count=len(idxs), metadata=meta)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    doc = (
        "Ο ασθενής ανέφερε οσφυαλγία μετά από άρση βάρους. Η αξιολόγηση έδειξε μυϊκό σπασμό. "
        "Το πλάνο θεραπείας περιλαμβάνει οκτώ συνεδρίες φυσικοθεραπείας και ασκήσεις ενδυνάμωσης.\n\n"
        "Όσον αφορά τα οικονομικά, το τιμολόγιο εκδόθηκε με ΦΠΑ 0% λόγω ιατρικής εξαίρεσης. "
        "Η πληρωμή έγινε με κάρτα και το υπόλοιπο μηδενίστηκε."
    )
    ch = SemanticChunker(max_tokens=60, min_tokens=15, similarity_threshold=0.30)
    res = ch.chunk_sync(doc)
    print(f"chunks={res.total_chunks} | embedder={type(ch.embedder).__name__} | doc_type={res.chunks[0].metadata.doc_type}")
    for c in res.chunks:
        print(f"  [{c.index}] tok~{c.token_estimate} sents={c.sentence_count}: {c.text[:70]}…")
