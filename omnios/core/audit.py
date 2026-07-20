"""
audit.py — Tamper-evident, hash-chained audit log (EU AI Act Art. 12 traceability).
===================================================================================
Every governance-relevant event (PII tokenization, re-identification, booking,
policy denial) is appended as a record whose hash chains to the previous one:

    h_i = HMAC_SHA256(secret, h_{i-1} || canonical_json(event_i))

Any modification, insertion or deletion of a past record breaks the chain, which
`verify()` detects. Records store ONLY tokenised data — never raw PII.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List

_AUDIT_SECRET = secrets.token_bytes(32)
_GENESIS = "0" * 64


@dataclass
class AuditRecord:
    seq: int
    event: str
    data: Dict[str, Any]
    prev_hash: str
    this_hash: str


class AuditLog:
    def __init__(self) -> None:
        self._records: List[AuditRecord] = []
        self._sinks: List = []          # e.g. PersistentAuditLog.record — survives restart
        self._lock = threading.Lock()   # η αλυσίδα είναι shared· concurrent append χωρίς lock τη χαλάει

    def add_sink(self, fn) -> None:
        """Attach a durable sink; every record is mirrored to it (best-effort, never blocks)."""
        self._sinks.append(fn)

    def _hash(self, prev_hash: str, payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hmac.new(_AUDIT_SECRET, (prev_hash + canonical).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def record(self, event: str, data: Dict[str, Any]) -> AuditRecord:
        # ΑΤΟΜΙΚΟ: seq→prev→hash→append κάτω από lock, αλλιώς δύο νήματα διαβάζουν το ίδιο prev/seq
        # και σπάει η hash-chain (verify() αποτυγχάνει). Το sink dispatch μένει ΜΕΣΑ στο lock ώστε
        # η σειρά προς τον durable sink να ταιριάζει με τη σειρά της αλυσίδας.
        with self._lock:
            seq = len(self._records)
            prev = self._records[-1].this_hash if self._records else _GENESIS
            payload = {"seq": seq, "event": event, "data": data}
            rec = AuditRecord(seq, event, data, prev, self._hash(prev, payload))
            self._records.append(rec)
            for sink in self._sinks:        # durable mirror (own chain in SQLite, stable secret)
                try:
                    sink(event, data)
                except Exception:
                    pass                    # a sink failure must never block the operational path
            return rec

    def verify(self) -> bool:
        """Re-derive the whole chain; returns False if any record was tampered."""
        with self._lock:                    # σταθερό snapshot ενώ επαληθεύουμε
            records = list(self._records)
        prev = _GENESIS
        for i, rec in enumerate(records):
            if rec.seq != i or rec.prev_hash != prev:
                return False
            expected = self._hash(prev, {"seq": rec.seq, "event": rec.event, "data": rec.data})
            if expected != rec.this_hash:
                return False
            prev = rec.this_hash
        return True

    def tail(self, n: int = 20) -> List[Dict[str, Any]]:
        return [
            {"seq": r.seq, "event": r.event, "data": r.data, "hash": r.this_hash[:12]}
            for r in self._records[-n:]
        ]

    def chain(self, n: int = 40) -> List[Dict[str, Any]]:
        """Fuller view for the audit UI: each record carries its own hash AND the prev-hash it
        links to, so the tamper-evident chain is directly visualizable (genesis → … → head)."""
        return [
            {"seq": r.seq, "event": r.event, "data": r.data,
             "hash": r.this_hash[:16], "prev": (r.prev_hash[:16] if r.seq else "genesis")}
            for r in self._records[-n:]
        ]

    def __len__(self) -> int:
        return len(self._records)


AUDIT = AuditLog()
