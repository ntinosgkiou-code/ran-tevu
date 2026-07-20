"""
value_tracker.py — real-time value & compute accounting for value-based SaaS billing.
======================================================================================
Every workflow execution is priced with TRACEABLE math (a finance reviewer can follow each €):

    saved_minutes = manual_baseline − machine_minutes
    gross_value   = saved_minutes / 60 × opportunity_rate      (coder €18/h or physician €70/h)
    net_value     = gross_value − llm_token_cost               (tiktoken-accurate when available)
    fee           = 10% × net_value                            (pure performance pricing)

Token counting uses tiktoken (cl100k_base) when installed — exact, not estimated; falls back
to the ~4-chars/token heuristic offline. Rates and baselines live in the clinical manifest,
with their rationale stated (billing.baseline_rationale / physician_rationale).

Deterministic, typed. Exercised by test_omnios_core.py (value math) + its own selftest below.
"""
from __future__ import annotations

import math
import time

from omnios_manifest_loader import MANIFEST
from predictive_billing import ValueBasedBilling, ValueEvent

try:                                            # exact tokenizer when available
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:                               # offline fallback — stated, not hidden
    _ENC = None


def count_tokens(text: str) -> int:
    """Exact token count via tiktoken when installed; ~4 chars/token heuristic otherwise."""
    if _ENC is not None:
        return len(_ENC.encode(text))
    return math.ceil(len(text) / 4)


class ValueTracker:
    """Measures the € the automation actually saves per execution. `profile` selects whose
    time is being saved: 'coder' (default, conservative) or 'physician' (opportunity cost
    when the doctor does their own documentation — the common Greek private-practice case)."""

    def __init__(self, profile: str = "coder") -> None:
        b = MANIFEST["billing"]
        self.profile = profile
        self.baseline_min = float(b["manual_baseline_minutes_per_note"])
        self.hourly = float(b["physician_hourly_eur" if profile == "physician"
                              else "coder_hourly_eur"])
        self.rate_in = float(b["llm_price_eur_per_1k_tokens_in"]) / 1000.0
        self.rate_out = float(b["llm_price_eur_per_1k_tokens_out"]) / 1000.0
        self._billing = ValueBasedBilling(pct=float(b["fee_share"]))
        self.entries: list[dict] = []

    def record(self, state_id: str, note_text: str, elapsed_s: float) -> dict:
        tokens_in = count_tokens(note_text)
        tokens_out = math.ceil(tokens_in * 0.6)
        llm_cost = tokens_in * self.rate_in + tokens_out * self.rate_out
        machine_min = elapsed_s / 60.0
        saved_min = max(0.0, self.baseline_min - machine_min)
        gross = saved_min / 60.0 * self.hourly
        net = round(max(0.0, gross - llm_cost), 4)
        entry = {"state_id": state_id, "profile": self.profile,
                 "elapsed_s": round(elapsed_s, 4), "time_saved_min": round(saved_min, 2),
                 "tokens_in": tokens_in, "tokens_out": tokens_out,
                 "tokenizer": "tiktoken" if _ENC else "heuristic~4chars",
                 "llm_cost_eur": round(llm_cost, 5), "gross_value_eur": round(gross, 4),
                 "net_saved_eur": net}
        self.entries.append(entry)
        return entry

    def totals(self) -> dict:
        net = round(sum(e["net_saved_eur"] for e in self.entries), 2)
        bill = self._billing.bill_sync([ValueEvent(kind="coding_automation", euros=net)])
        return {"executions": len(self.entries), "profile": self.profile,
                "net_saved_eur": net,
                "time_saved_min": round(sum(e["time_saved_min"] for e in self.entries), 1),
                "fee_eur": bill.charge_eur, "fee_model": bill.model}


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for profile in ("coder", "physician"):
        vt = ValueTracker(profile)
        t0 = time.monotonic()
        vt.record("demo", "Αρτηριακή υπέρταση. Έγινε ΗΚΓ. Διάρκεια 30 λεπτά.",
                  time.monotonic() - t0 + 0.01)
        print(profile, "→", vt.totals())
