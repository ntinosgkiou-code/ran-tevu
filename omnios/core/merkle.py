"""
merkle.py — Merkle-tree anchoring + inclusion proofs over the audit chain.
==========================================================================
Research grounding (verified citations in RESEARCH.md):
  • R. Merkle, "A Digital Signature Based on a Conventional Encryption Function", CRYPTO 1987 —
    the Merkle hash tree.
  • S. Haber & W.S. Stornetta, "How to Time-Stamp a Digital Document", J. Cryptology 1991 —
    linked timestamping (our HMAC hash chain already implements this linking).
  • RFC 6962 (Certificate Transparency) — the production pattern we mirror: a verifiable
    append-only log with Merkle inclusion proofs.

What this adds on top of the existing hash chain: the ENTIRE audit history compresses to one
32-byte ROOT. A clinic can publish that root externally (monthly report, email, notary) and later
PROVE any single record belongs to the anchored history — with an O(log n) inclusion proof that
reveals NOTHING about the other records. Tamper-evidence becomes third-party-verifiable.

Domain separation (leaf vs node prefixes 0x00/0x01, as in RFC 6962 §2.1) prevents second-preimage
splices between leaves and internal nodes. Deterministic, stdlib-only.
Tests: python core/test_merkle.py
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

_LEAF = b"\x00"
_NODE = b"\x01"


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def leaf_hash(record: dict) -> str:
    """RFC 6962-style leaf hash: H(0x00 || canonical(record))."""
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return _h(_LEAF + canonical.encode("utf-8"))


def _node_hash(left: str, right: str) -> str:
    return _h(_NODE + bytes.fromhex(left) + bytes.fromhex(right))


class MerkleTree:
    """Merkle tree over an ordered list of records (audit chain entries). Odd nodes are
    promoted (Bitcoin-style duplication is avoided — promotion has no ambiguity)."""

    def __init__(self, records: list[dict]) -> None:
        self.leaves: list[str] = [leaf_hash(r) for r in records]
        self.levels: list[list[str]] = [list(self.leaves)]
        level = self.leaves
        while len(level) > 1:
            nxt: list[str] = []
            for i in range(0, len(level) - 1, 2):
                nxt.append(_node_hash(level[i], level[i + 1]))
            if len(level) % 2 == 1:
                nxt.append(level[-1])            # promote the odd node unchanged
            self.levels.append(nxt)
            level = nxt

    @property
    def root(self) -> Optional[str]:
        return self.levels[-1][0] if self.leaves else None

    def inclusion_proof(self, index: int) -> list[dict]:
        """The O(log n) sibling path proving leaves[index] is under root. Each step:
        {'hash': sibling, 'side': 'left'|'right'} — the side the SIBLING sits on."""
        if not 0 <= index < len(self.leaves):
            raise IndexError(index)
        proof: list[dict] = []
        idx = index
        for level in self.levels[:-1]:
            sib = idx ^ 1                        # the paired sibling
            if sib < len(level):
                proof.append({"hash": level[sib], "side": "left" if sib < idx else "right"})
            # odd promoted node has no sibling at this level → no step appended
            idx //= 2
        return proof


def verify_inclusion(record: dict, proof: list[dict], root: str) -> bool:
    """Third-party verification: recompute from the record up the sibling path → must hit root.
    Needs ONLY the record, the proof and the published root — no access to the rest of the log."""
    cur = leaf_hash(record)
    for step in proof:
        if step["side"] == "left":
            cur = _node_hash(step["hash"], cur)
        else:
            cur = _node_hash(cur, step["hash"])
    return cur == root


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    records = [{"seq": i, "event": "e", "data": {"i": i}} for i in range(7)]
    t = MerkleTree(records)
    print("root:", t.root[:16], "… over", len(records), "records")
    proof = t.inclusion_proof(4)
    print("proof for #4:", len(proof), "hashes · verifies:", verify_inclusion(records[4], proof, t.root))
    records[4]["data"]["i"] = 999
    print("after tamper, verifies:", verify_inclusion(records[4], proof, t.root))
