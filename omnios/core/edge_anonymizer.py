"""
edge_anonymizer.py — STEP 1: Edge PII Anonymization Layer.
=========================================================
Removes / pseudonymizes personal data **locally (at the edge)** before any text is
sent to a commercial LLM — GDPR Art. 25 & 32 (data protection by design), HIPAA
Safe-Harbor de-identification. The token → value reverse-map never leaves this process.

Design (why it is production-grade, not a wrapper):
  • Deterministic, *reversible* pseudonymization with human-readable tokens
    ([REDACTED_NAME_1], [REDACTED_AMKA_1], …) — same value → same token within a payload.
  • High-precision structured detectors WITH validation (Luhn for cards, mod-97 for IBAN,
    date-shaped ΑΜΚΑ) so we don't shred legitimate text (false positives are a real cost).
  • Overlap resolution: all candidate spans are found on the ORIGINAL text, conflicts are
    resolved by priority+length, then spliced right-to-left → correct, non-corrupting output.
  • Dates are OFF by default (a booking message's "12/03/2026" is an appointment, not a DOB);
    enable `redact_dates=True` only on the clinical-record path.
  • Optional spaCy NER for names-without-a-cue (extra recall); **graceful offline fallback**
    to a Greek/English cue detector when spaCy is absent — the stack stays deterministic.
  • Async API (CPU work offloaded via asyncio.to_thread), Pydantic v2 validation, full typing.

Self-test / tests:  python core/test_edge_anonymizer.py
"""
from __future__ import annotations

import asyncio
import re
from enum import Enum
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict, Field


# ─────────────────────────── models ───────────────────────────
class PIIKind(str, Enum):
    NAME = "NAME"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    AMKA = "AMKA"        # Greek social-security number
    IBAN = "IBAN"
    CARD = "CARD"        # payment card (Luhn-valid)
    ID = "ID"            # Greek ID card
    DOB = "DOB"          # date of birth (opt-in)


# Overlap priority: lower wins when spans collide (structured IDs beat a loose name match).
_PRIORITY: dict[PIIKind, int] = {
    PIIKind.EMAIL: 0, PIIKind.IBAN: 1, PIIKind.CARD: 2, PIIKind.AMKA: 3,
    PIIKind.ID: 4, PIIKind.PHONE: 5, PIIKind.DOB: 6, PIIKind.NAME: 7,
}


class PIIEntity(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: PIIKind
    token: str
    original: str = Field(repr=False)   # kept out of repr so it doesn't leak into logs
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class AnonymizationResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    redacted: str
    entities: list[PIIEntity]

    @property
    def pii_count(self) -> int:
        return len(self.entities)

    def mapping(self) -> dict[str, str]:
        """token → original. Stays LOCAL; feed it back to deanonymize() on egress."""
        return {e.token: e.original for e in self.entities}


# ─────────────────────────── detectors ───────────────────────────
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_RE_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_RE_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_RE_AMKA = re.compile(r"(?<!\d)\d{11}(?!\d)")
_RE_ID = re.compile(r"\b[A-ZΑ-ΩΆΈΉΊΌΎΏ]{1,2}[ -]?\d{6}\b")
_RE_PHONE = re.compile(r"(?<![\w])(\+?\d[\d\s().\-]{8,}\d)(?![\w])")
_RE_DOB = re.compile(r"\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\b")

_CAP = r"[A-ZΑ-ΩΆΈΉΊΌΎΏ][A-Za-zΑ-Ωα-ωΆΈΉΊΌΎΏάέήίόύώϊϋΐΰ.'\-]+"
# Cue keywords are case-insensitive (scoped (?i:…)); the name capture stays CASE-SENSITIVE so it
# only grabs Capitalized words — a global IGNORECASE would let _CAP match lowercase words too
# (e.g. "και", "λέγομαι") and over-redact.
_RE_NAME_CUE = re.compile(
    # Clinical cues are WORD-BOUNDED (\b / lookbehind) so "κ." never matches the tail of "έλκος." —
    # a loose cue once redacted the capitalized word after "έλκος. Το…" and corrupted the medical KB.
    r"(?i:είμαι\s+(?:ο|η)\s+|ονομάζομαι\s+|λέγομαι\s+|όνομα[:\s]+|"
    r"\bασθεν(?:ής|ης|ούς|ους)\s+|\bκ(?:ύριος|υρία)\s+|(?<!\w)κ\.\s+|"
    r"(?:i\s*am|my\s+name\s+is|this\s+is|patient)\s+)"
    r"(" + _CAP + r"(?:\s+" + _CAP + r"){0,2})",
    re.UNICODE,
)
_NER_NAME_LABELS = {"PERSON", "PER"}


def _luhn_ok(value: str) -> bool:
    digits = [int(c) for c in value if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def _iban_ok(value: str) -> bool:
    v = value.strip().upper()
    if not 15 <= len(v) <= 34:
        return False
    rearranged = v[4:] + v[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)  # letters → 10..35
    try:
        return int(digits) % 97 == 1
    except ValueError:
        return False


def _amka_ok(value: str) -> bool:
    if len(set(value)) == 1:        # 00000000000 etc. → not a real ΑΜΚΑ
        return False
    dd, mm = int(value[0:2]), int(value[2:4])
    return 1 <= dd <= 31 and 1 <= mm <= 12   # first 6 digits are a DDMMYY birth date


# ─────────────────────────── the layer ───────────────────────────
class EdgeAnonymizer:
    """Local PII pseudonymizer. Construct once, reuse. Thread-safe for read-only use."""

    def __init__(self, use_ner: bool = True, redact_dates: bool = False) -> None:
        self.redact_dates = redact_dates
        self._nlp = self._load_ner() if use_ner else None

    @staticmethod
    def _load_ner():
        try:
            import spacy  # optional heavy dependency
        except Exception:
            return None
        for model in ("el_core_news_sm", "xx_ent_wiki_sm", "en_core_web_sm"):
            try:
                return spacy.load(model)
            except Exception:
                continue
        return None

    @property
    def ner_active(self) -> bool:
        return self._nlp is not None

    def _detectors(self) -> list[tuple[PIIKind, "re.Pattern", Optional[Callable[[str], bool]]]]:
        dets: list[tuple[PIIKind, "re.Pattern", Optional[Callable[[str], bool]]]] = [
            (PIIKind.EMAIL, _RE_EMAIL, None),
            (PIIKind.IBAN, _RE_IBAN, _iban_ok),
            (PIIKind.CARD, _RE_CARD, _luhn_ok),
            (PIIKind.AMKA, _RE_AMKA, _amka_ok),
            (PIIKind.ID, _RE_ID, None),
            (PIIKind.PHONE, _RE_PHONE, lambda s: len(re.sub(r"\D", "", s)) >= 10),
        ]
        if self.redact_dates:
            dets.append((PIIKind.DOB, _RE_DOB, None))
        return dets

    # ---- public async API ----
    async def anonymize(self, text: str) -> AnonymizationResult:
        return await asyncio.to_thread(self.anonymize_sync, text)

    async def deanonymize(self, text: str, mapping: dict[str, str]) -> str:
        return await asyncio.to_thread(self.deanonymize_sync, text, mapping)

    # ---- sync core (also directly usable) ----
    def anonymize_sync(self, text: str) -> AnonymizationResult:
        if not isinstance(text, str):
            raise TypeError(f"text must be str, got {type(text).__name__}")
        if not text:
            return AnonymizationResult(redacted="", entities=[])

        # 1) collect candidate spans on the ORIGINAL text
        cands: list[tuple[int, int, PIIKind]] = []
        for kind, rx, validator in self._detectors():
            for m in rx.finditer(text):
                if validator is None or validator(m.group(0)):
                    cands.append((m.start(), m.end(), kind))
        # names: spaCy NER (recall) + cue detector (offline floor)
        if self._nlp is not None:
            try:
                for ent in self._nlp(text).ents:
                    if ent.label_ in _NER_NAME_LABELS:
                        cands.append((ent.start_char, ent.end_char, PIIKind.NAME))
            except Exception:
                pass
        for m in _RE_NAME_CUE.finditer(text):
            cands.append((m.start(1), m.end(1), PIIKind.NAME))

        # 2) resolve overlaps: prefer earlier start, then longer span, then higher priority
        cands.sort(key=lambda t: (t[0], -(t[1] - t[0]), _PRIORITY[t[2]]))
        chosen: list[tuple[int, int, PIIKind]] = []
        last_end = -1
        for start, end, kind in cands:
            if start >= last_end:
                chosen.append((start, end, kind))
                last_end = end

        # 3) assign readable, reversible, de-duplicated tokens
        counters: dict[PIIKind, int] = {}
        valmap: dict[tuple[PIIKind, str], str] = {}
        entities: list[PIIEntity] = []
        for start, end, kind in chosen:
            val = text[start:end]
            key = (kind, val)
            token = valmap.get(key)
            if token is None:
                counters[kind] = counters.get(kind, 0) + 1
                token = f"[REDACTED_{kind.value}_{counters[kind]}]"
                valmap[key] = token
            entities.append(PIIEntity(kind=kind, token=token, original=val, start=start, end=end))

        # 4) splice right-to-left so earlier spans keep their offsets
        redacted = text
        for e in sorted(entities, key=lambda e: e.start, reverse=True):
            redacted = redacted[: e.start] + e.token + redacted[e.end:]

        return AnonymizationResult(redacted=redacted, entities=entities)

    @staticmethod
    def deanonymize_sync(text: str, mapping: dict[str, str]) -> str:
        # longest tokens first so [REDACTED_NAME_11] isn't clobbered by [REDACTED_NAME_1]
        for token, original in sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True):
            text = text.replace(token, original)
        return text


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    az = EdgeAnonymizer()
    sample = ("Είμαι ο Γιώργος Παπαδόπουλος, ΑΜΚΑ 12036512345, κάρτα 4539 1488 0343 6467, "
              "IBAN GR1601101250000000012300695, τηλ 6945123456, email g.p@clinic.gr")
    res = az.anonymize_sync(sample)
    print("REDACTED:", res.redacted)
    print("PII found:", res.pii_count, "| NER:", az.ner_active)
    print("RESTORED == original:", az.deanonymize_sync(res.redacted, res.mapping()) == sample)
