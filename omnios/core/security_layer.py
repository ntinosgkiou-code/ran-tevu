"""
security_layer.py — the async security facade over the hardened edge-PII stack.
================================================================================
Single injection point (Dependency-Injection seam) for everything security: async
sanitize → bidirectional deterministic re-hydration → egress firewall. Deliberately a THIN
facade over the already-hardened, fully-tested `edge_anonymizer` + `PIIVault` — duplicating
detector logic here would create two security truths, which is how systems rot.

Honest scope note: spaCy transformer NER is an OPTIONAL hook inside EdgeAnonymizer (enabled
when a model is installed); on CPU-only deployments the deterministic detector stack
(regex + Luhn + IBAN mod-97 + ΑΜΚΑ validators) is the production path — and the tested one.

Tests: exercised by test_omnios_core.py / test_api_deeptech.py; selftest below.
"""
from __future__ import annotations

import asyncio

from omnios_core import PIIVault, EgressViolation   # re-export the single vault implementation

__all__ = ["AsyncSecurityLayer", "EgressViolation"]


class AsyncSecurityLayer:
    """async sanitize/rehydrate/assert-no-egress — one object, injected wherever text flows."""

    def __init__(self) -> None:
        self._vault = PIIVault()

    async def sanitize(self, text: str) -> tuple[str, str]:
        """Tokenize PII locally (thread-offloaded). Returns (redacted_text, receipt)."""
        return await asyncio.to_thread(self._vault.seal, text)

    async def rehydrate(self, text: str, receipt: str) -> str:
        """Deterministically restore tokens → original values, post-inference, locally."""
        return await asyncio.to_thread(self._vault.unseal, text, receipt)

    async def assert_no_pii(self, payload: str) -> None:
        """Egress firewall: raises EgressViolation if ANY detectable PII is outbound."""
        await asyncio.to_thread(self._vault.assert_no_pii, payload)

    @property
    def tokens_issued(self) -> int:
        return self._vault.tokens_issued


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    async def _demo() -> None:
        sec = AsyncSecurityLayer()
        raw = "Ο Γιώργος Παπαδόπουλος, ΑΜΚΑ 12036512345, τηλ 6945123456."
        red, receipt = await sec.sanitize(raw)
        print("sanitized:", red)
        await sec.assert_no_pii(red)
        print("egress firewall: PASS (zero PII outbound)")
        back = await sec.rehydrate(red, receipt)
        print("bidirectional restore exact:", back == raw)

    asyncio.run(_demo())
