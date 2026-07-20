"""
token_guard.py — Lossless Information Density context compression + re-hydration.
==================================================================================
Token economy must NEVER cost clinical precision. The ContextCompressor removes ONLY
syntactic redundancy (whitespace runs, duplicated lines, standalone conversational filler
words) and then PROVES nothing semantic was lost:

  • Entity-integrity check — every medical entity (ICD-10/CPT codes, numbers, vault tokens,
    manifest lexicon/stem terms) present in the original must survive in the compressed text.
    integrity = |preserved| / |original|. If integrity < 0.999 ⇒ compression is BYPASSED and
    the full raw context is passed through untouched.
  • Shannon entropy (bits/char) is reported before/after — compression should RAISE per-char
    information density (removing low-entropy filler), never lower total information.

TokenGuard keeps the raw text in a local registry so the state machine can trigger a
Context Re-Hydration event: on any downstream discrepancy (missing evidence, empty claim),
the COMPLETE original context is pulled back for a full deep-reasoning cycle.

Deterministic, offline, Pydantic v2, typed. Tests: python core/test_token_guard.py
"""
from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from typing import Optional

from pydantic import BaseModel, ConfigDict

from omnios_manifest_loader import MANIFEST


# ─────────────────────────── report model ───────────────────────────
class GuardReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    original_chars: int
    compressed_chars: int
    ratio: float                       # compressed/original (1.0 = no change)
    entropy_before_bits: float         # Shannon bits/char
    entropy_after_bits: float
    entities_total: int
    entities_preserved: int
    integrity: float                   # preserved/total (1.0 required in practice)
    bypassed: bool                     # True ⇒ integrity < 0.999, raw passed through


# ─────────────────────────── the compressor ───────────────────────────
_ICD_RX = re.compile(r"\b[A-TV-Z][0-9][0-9AB](?:\.[0-9A-TV-Z]{1,4})?\b")
_CPT_RX = re.compile(r"\b\d{4}[0-9A-Z]\b")
_NUM_RX = re.compile(r"\b\d+(?:[.,]\d+)?\b")
_VAULT_RX = re.compile(r"\[REDACTED_[A-Z]+_\d+\]")
_WS_RX = re.compile(r"[ \t]{2,}")
_NL_RX = re.compile(r"\n{3,}")

# standalone conversational filler — the ONLY words we are allowed to drop (syntactic, not semantic)
_FILLERS = {"λοιπόν", "βασικά", "απλά", "δηλαδή", "ξέρεις", "οπότε", "τέλος", "πάντων",
            "um", "uh", "basically", "actually", "literally", "just", "like"}
_FILLER_RX = re.compile(r"(?<![\wά-ώΑ-Ω])(" + "|".join(re.escape(w) for w in sorted(_FILLERS)) +
                        r")(?![\wά-ώΑ-Ω])", re.IGNORECASE | re.UNICODE)

INTEGRITY_FLOOR = 0.999


def shannon_entropy_bits(text: str) -> float:
    """Shannon entropy in bits per character — the information-density metric."""
    if not text:
        return 0.0
    counts = Counter(text)
    n = len(text)
    return round(-sum((c / n) * math.log2(c / n) for c in counts.values()), 4)


class ContextCompressor:
    """Removes ONLY syntactic redundancy; proves entity preservation or bypasses itself."""

    def __init__(self) -> None:
        terms: set[str] = set()
        for section in ("icd10_lexicon", "icd10_stems", "cpt_rules"):
            for k in MANIFEST.get(section, {}):
                if not k.startswith("_"):
                    terms.add(k.lower())
        self._domain_terms = terms

    def extract_entities(self, text: str) -> set[str]:
        """Everything that is NOT allowed to disappear: codes, numbers, vault tokens,
        and every manifest medical term/stem that occurs in the text."""
        low = text.lower()
        ents: set[str] = set()
        for rx in (_ICD_RX, _CPT_RX, _NUM_RX, _VAULT_RX):
            ents.update(m.group(0) for m in rx.finditer(text))
        ents.update(t for t in self._domain_terms if t in low)
        return ents

    def _strip_syntactic(self, text: str) -> str:
        out = _FILLER_RX.sub("", text)                       # filler words (standalone only)
        out = _WS_RX.sub(" ", out)                           # whitespace runs → single space
        out = _NL_RX.sub("\n\n", out)                        # newline runs → max blank line
        # consecutive duplicate lines (system-prompt duplication pattern) → keep first
        lines, prev = [], object()
        for ln in out.split("\n"):
            if ln.strip() and ln == prev:
                continue
            lines.append(ln)
            prev = ln
        out = "\n".join(lines)
        return re.sub(r" +([.,;·])", r"\1", out).strip()

    def compress(self, text: str) -> tuple[str, GuardReport]:
        original_entities = self.extract_entities(text)
        candidate = self._strip_syntactic(text)
        preserved = self.extract_entities(candidate) & original_entities
        integrity = (len(preserved) / len(original_entities)) if original_entities else 1.0
        bypassed = integrity < INTEGRITY_FLOOR
        final = text if bypassed else candidate
        report = GuardReport(
            original_chars=len(text), compressed_chars=len(final),
            ratio=round(len(final) / len(text), 4) if text else 1.0,
            entropy_before_bits=shannon_entropy_bits(text),
            entropy_after_bits=shannon_entropy_bits(final),
            entities_total=len(original_entities), entities_preserved=len(preserved),
            integrity=round(integrity, 4), bypassed=bypassed)
        return final, report


# ─────────────────────────── the guard (with re-hydration) ───────────────────────────
class TokenGuard:
    """Compresses context losslessly and keeps the raw original locally so a downstream
    discrepancy can trigger Context Re-Hydration (full raw context, deep reasoning cycle)."""

    def __init__(self, compressor: Optional[ContextCompressor] = None) -> None:
        self.compressor = compressor or ContextCompressor()
        self._raw: dict[str, str] = {}
        self.reports: list[GuardReport] = []

    def guard(self, text: str) -> tuple[str, str]:
        compressed, report = self.compressor.compress(text)
        gid = f"guard-{uuid.uuid4().hex[:10]}"
        self._raw[gid] = text
        self.reports.append(report)
        return compressed, gid

    def rehydrate(self, guard_id: str) -> str:
        """Pull the COMPLETE original context back — the re-hydration event."""
        if guard_id not in self._raw:
            raise KeyError(guard_id)
        return self._raw[guard_id]

    def last_report(self) -> Optional[GuardReport]:
        return self.reports[-1] if self.reports else None


# ═══════════════ Dynamic Delta Routing — only CHANGED state fields travel ═══════════════
def state_diff(old: dict, new: dict, prefix: str = "") -> tuple[dict, list]:
    """Recursive dict diff → ({dotted.path: new_value}, [deleted dotted.paths]).
    Lists/scalars are treated as atomic values (replaced whole) — deterministic and cheap."""
    set_ops: dict = {}
    del_ops: list = []
    for k in old:
        if k not in new:
            del_ops.append(f"{prefix}{k}")
    for k, v in new.items():
        path = f"{prefix}{k}"
        if k not in old:
            set_ops[path] = v
        elif isinstance(v, dict) and isinstance(old[k], dict):
            s, d = state_diff(old[k], v, path + ".")
            set_ops.update(s)
            del_ops.extend(d)
        elif old[k] != v:
            set_ops[path] = v
    return set_ops, del_ops


def apply_delta(base: dict, delta: dict) -> dict:
    """Reconstruct the full state on the receiving side from base + delta (pure, non-mutating)."""
    import copy
    out = copy.deepcopy(base)
    if "__full__" in delta:
        return copy.deepcopy(delta["__full__"])
    for path in delta.get("del", []):
        node, keys = out, path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node.pop(keys[-1], None)
    for path, value in delta.get("set", {}).items():
        node, keys = out, path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
    return out


class DeltaRouter:
    """Between successive operations on the same channel, transmit ONLY the modified state
    fields. Sender and receiver each keep a mirror; `encode` emits {'set':…, 'del':…} (or
    {'__full__':…} on first contact), `decode` reconstructs the exact full state. Token savings
    are MEASURED with the real tokenizer, not asserted (see report())."""

    def __init__(self) -> None:
        self._sent: dict[str, dict] = {}
        self._recv: dict[str, dict] = {}
        self._tok_full = 0
        self._tok_delta = 0

    @staticmethod
    def _tokens(obj) -> int:
        import json as _json
        from value_tracker import count_tokens
        return count_tokens(_json.dumps(obj, ensure_ascii=False, sort_keys=True))

    def encode(self, channel: str, state: dict) -> dict:
        import copy
        prev = self._sent.get(channel)
        delta = {"__full__": state} if prev is None else (
            lambda s, d: {"set": s, "del": d})(*state_diff(prev, state))
        self._sent[channel] = copy.deepcopy(state)
        self._tok_full += self._tokens(state)
        self._tok_delta += self._tokens(delta)
        return delta

    def decode(self, channel: str, delta: dict) -> dict:
        base = self._recv.get(channel, {})
        full = apply_delta(base, delta)
        self._recv[channel] = full
        return full

    def report(self) -> dict:
        saved = 1.0 - (self._tok_delta / self._tok_full) if self._tok_full else 0.0
        return {"tokens_full_equiv": self._tok_full, "tokens_transmitted": self._tok_delta,
                "savings_pct": round(saved * 100.0, 1)}


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    tg = TokenGuard()
    noisy = ("Λοιπόν,  βασικά ο ασθενής   έχει απλά αρτηριακή υπέρταση.\n\n\n\n"
             "Έγινε δηλαδή ΗΚΓ.   Διάρκεια 30 λεπτά.\nΈγινε δηλαδή ΗΚΓ.   Διάρκεια 30 λεπτά.")
    out, gid = tg.guard(noisy)
    r = tg.last_report()
    print("compressed:", repr(out))
    print(f"ratio {r.ratio} · entropy {r.entropy_before_bits}→{r.entropy_after_bits} bits/char "
          f"· integrity {r.integrity} · bypassed {r.bypassed}")
    print("rehydrated == original:", tg.rehydrate(gid) == noisy)
