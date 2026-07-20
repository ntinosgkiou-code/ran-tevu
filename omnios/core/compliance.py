"""
compliance.py — Zero-Trust tokenization vault (GDPR Art. 25/32, EU AI Act).
PII (name/phone/email) is replaced on ingress by HMAC-derived tokens; the
token→value map lives in an isolated in-process vault and is never given to the
booking actor. Re-identification happens once, at egress.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_VAULT_SECRET: bytes = secrets.token_bytes(32)

_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_RE_PHONE = re.compile(r"(?<![\w])(\+?\d[\d\s().\-]{8,}\d)(?![\w])")
_CAP = r"[A-ZΑ-ΩΆΈΉΊΌΎΏ][A-Za-zΑ-Ωα-ωΆΈΉΊΌΎΏάέήίόύώϊϋΐΰ.'\-]+"
_RE_NAME_CUE = re.compile(
    r"(?:είμαι\s+(?:ο|η)\s+|ονομάζομαι\s+|λέγομαι\s+|όνομα[:\s]+|"
    r"(?:i\s*am|my\s+name\s+is|this\s+is)\s+)"
    r"(" + _CAP + r"(?:\s+" + _CAP + r"){0,2})",
    re.UNICODE | re.IGNORECASE,
)

# High-precision structured-PII detectors (Phase 0). UNAMBIGUOUS identifiers, safe to tokenize
# on ingress. Note: DATES/DOB are deliberately NOT tokenized here — in a booking message a date
# is an appointment time, not a birth date; DOB is handled only in the clinical-scribe path.
_RE_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_RE_AMKA = re.compile(r"(?<!\d)\d{11}(?!\d)")   # Greek social-security number (ΑΜΚΑ) = DDMMYY + 5


def _llm_detect_names(text: str) -> list:
    """Optional high-recall detector for names WITHOUT a conversational cue. No-op unless an LLM
    backend is wired — keeps the stack deterministic/offline by default. The regex cue-detector
    stays the precision floor; this only ADDS recall (the flagged weak spot) when enabled."""
    return []


@dataclass
class TokenVault:
    _map: Dict[str, str] = field(default_factory=dict)
    _reverse: Dict[str, str] = field(default_factory=dict)
    _kind: Dict[str, str] = field(default_factory=dict)
    pii_count: int = 0

    def tokenize(self, value: str, kind: str) -> str:
        value = value.strip()
        if not value:
            return value
        if value in self._reverse:
            return self._reverse[value]
        token = self._mint(value, kind)
        self._map[token] = value
        self._reverse[value] = token
        self._kind[token] = kind
        self.pii_count += 1
        return token

    def _mint(self, value: str, kind: str, salt: str = "") -> str:
        digest = hmac.new(_VAULT_SECRET, f"{kind}:{value}:{salt}".encode("utf-8"),
                          hashlib.sha256).hexdigest()[:8].upper()
        token = f"[TOKEN_{digest}]"
        if token in self._map and self._map[token] != value:
            return self._mint(value, kind, salt=secrets.token_hex(2))
        return token

    def resolve(self, token: str) -> Optional[str]:
        return self._map.get(token)

    def tokens(self) -> List[str]:
        return list(self._map.keys())

    def tokens_of_kind(self, kind: str) -> List[str]:
        return [t for t, k in self._kind.items() if k == kind]


def anonymize_payload(text: str) -> Tuple[str, TokenVault]:
    vault = TokenVault()
    redacted = _RE_EMAIL.sub(lambda m: vault.tokenize(m.group(0), "EMAIL"), text)
    redacted = _RE_IBAN.sub(lambda m: vault.tokenize(m.group(0), "IBAN"), redacted)
    redacted = _RE_AMKA.sub(lambda m: vault.tokenize(m.group(0), "AMKA"), redacted)

    def _phone_sub(mo):
        raw = mo.group(0)
        return vault.tokenize(raw.strip(), "PHONE") if len(re.sub(r"\D", "", raw)) >= 10 else raw

    redacted = _RE_PHONE.sub(_phone_sub, redacted)

    def _name_sub(mo):
        name = mo.group(1).strip()
        return mo.group(0).replace(name, vault.tokenize(name, "NAME"))

    redacted = _RE_NAME_CUE.sub(_name_sub, redacted)
    for _nm in _llm_detect_names(text):       # extra recall when an LLM backend is configured (no-op otherwise)
        if _nm and _nm in redacted:
            redacted = redacted.replace(_nm, vault.tokenize(_nm, "NAME"))
    return redacted, vault


def deanonymize_response(text: str, vault: TokenVault) -> Tuple[str, int]:
    restored, hits = text, 0
    for token in vault.tokens():
        if token in restored:
            restored = restored.replace(token, vault.resolve(token) or token)
            hits += 1
    return restored, hits
