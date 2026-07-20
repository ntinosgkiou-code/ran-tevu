"""
conformal.py — Split (inductive) conformal prediction for the coding gate.
==========================================================================
Research grounding (verified citations in RESEARCH.md):
  • Vovk, Gammerman, Shafer — "Algorithmic Learning in a Random World" (Springer 2005).
  • Papadopoulos et al. — inductive/split conformal (ECML 2002).
  • Angelopoulos & Bates — "A Gentle Introduction to Conformal Prediction..." (arXiv 2021).

THE point: our evidence scores stop being "vibes" and acquire a DISTRIBUTION-FREE, finite-sample
guarantee. Split conformal: take n calibration pairs (note, true code), compute nonconformity
s_i = 1 − score the pipeline assigns to the TRUE code (missed code ⇒ s_i = 1). Let

    q̂ = the ⌈(n+1)(1−α)⌉-th smallest of s_1..s_n.

Then for a NEW exchangeable pair, P( s_{n+1} ≤ q̂ ) ≥ 1−α — no distributional assumptions.
Operationally: gate floor = 1 − q̂; a true code scores below the floor with probability ≤ α.

HONEST SCOPE (stated, not hidden): the guarantee holds under exchangeability between calibration
and deployment notes. Our calibration set is synthetic-but-realistic Greek clinical shorthand; a
pilot's first labeled notes drop straight into CALIBRATION and the floor recalibrates — the MATH
stays identical. That is the whole design: the guarantee machinery is real and in place.

Deterministic. Tests: python core/test_conformal.py
"""
from __future__ import annotations

import math
from typing import Optional

from pydantic import BaseModel, ConfigDict


# ── labeled clinical shorthand: (note, {true codes}) ────────────────────────
# Built from the manifest's coding surface: exact-term notes, stem-inflection notes, and a few
# deliberately out-of-lexicon phrasings (the pipeline SHOULD miss those — that tail is exactly
# what pushes q̂ up and makes the guarantee honest instead of optimistic).
CALIBRATION: list[tuple[str, set[str]]] = [
    ("Αρτηριακή υπέρταση, σταθερή. Διάρκεια 20 λεπτά.", {"I10"}),
    ("Ασθενής με αρτηριακή υπέρταση. Έγινε ΗΚΓ. Διάρκεια 30 λεπτά.", {"I10", "93000"}),
    ("Οσφυαλγία μετά από άρση βάρους. Διάρκεια 25 λεπτά.", {"M54.5"}),
    ("Κεφαλαλγία από χθες, χωρίς εστιακά. Διάρκεια 15 λεπτά.", {"R51.9"}),
    ("Φαρυγγίτιδα με πυρετός 38.2. Έγινε γενική αίματος.", {"J02.9", "R50.9", "85025"}),
    ("Σακχαρώδης διαβήτης, ρύθμιση. Έγινε γενική αίματος. Διάρκεια 30 λεπτά.", {"E11.9", "85025"}),
    ("Ζάλη κατά την έγερση. Διάρκεια 20 λεπτά.", {"R42"}),
    ("Θωρακικό άλγος κατά την κόπωση. Έγινε ΗΚΓ.", {"R07.9", "93000"}),
    ("Δύσπνοια στην κόπωση. Έγινε σπιρομέτρηση.", {"R06.00", "94010"}),
    ("Αϋπνία τους τελευταίους μήνες. Διάρκεια 25 λεπτά.", {"G47.00"}),
    ("Γαστρίτιδα, καύσος επιγαστρίου. Διάρκεια 15 λεπτά.", {"K29.70"}),
    ("Αρτηριακή υπέρταση και σακχαρώδης διαβήτης. Διάρκεια 35 λεπτά.", {"I10", "E11.9"}),
    ("Κεφαλαλγία με ζάλη. Διάρκεια 20 λεπτά.", {"R51.9", "R42"}),
    ("Οσφυαλγία, βελτίωση. Διάρκεια 10 λεπτά.", {"M54.5"}),
    ("Φαρυγγίτιδα, ήπια. Διάρκεια 10 λεπτά.", {"J02.9"}),
    ("Θωρακικό άλγος και δύσπνοια. Έγινε ΗΚΓ και σπιρομέτρηση.", {"R07.9", "R06.00", "93000", "94010"}),
    ("Αρτηριακή υπέρταση. Έγινε γενική αίματος. Διάρκεια 30 λεπτά.", {"I10", "85025"}),
    ("Πυρετός τριημέρου. Έγινε γενική αίματος.", {"R50.9", "85025"}),
    ("Σακχαρώδης διαβήτης, επιδείνωση. Διάρκεια 30 λεπτά.", {"E11.9"}),
    ("Γαστρίτιδα και ζάλη. Διάρκεια 25 λεπτά.", {"K29.70", "R42"}),
    # stem-inflection tail (πιάνονται από stems με score 0.90)
    ("Αναφέρει αστάθεια στον διαβήτη του. Διάρκεια 20 λεπτά.", {"E11.9"}),
    ("Επεισόδια υπέρτασης το πρωί. Διάρκεια 20 λεπτά.", {"I10"}),
    ("Παρατεταμένος πυρετός χθες βράδυ.", {"R50.9"}),
    ("Έντονη κεφαλαλγίες καθημερινά. Διάρκεια 15 λεπτά.", {"R51.9"}),
    # deliberately OUT of the coding surface (the pipeline should miss → s = 1)
    ("Ημικρανία με αύρα δύο φορές τον μήνα.", {"G43.909"}),
    ("Κνησμός και εξάνθημα στα άνω άκρα.", {"L29.9"}),
]

HOLDOUT: list[tuple[str, set[str]]] = [
    ("Αρτηριακή υπέρταση. Διάρκεια 30 λεπτά.", {"I10"}),
    ("Οσφυαλγία μετά από πτώση. Διάρκεια 20 λεπτά.", {"M54.5"}),
    ("Φαρυγγίτιδα με πυρετός. Έγινε γενική αίματος.", {"J02.9", "R50.9", "85025"}),
    ("Θωρακικό άλγος. Έγινε ΗΚΓ. Διάρκεια 25 λεπτά.", {"R07.9", "93000"}),
    ("Σακχαρώδης διαβήτης. Διάρκεια 30 λεπτά.", {"E11.9"}),
    ("Δύσπνοια. Έγινε σπιρομέτρηση.", {"R06.00", "94010"}),
    ("Ζάλη και κεφαλαλγία. Διάρκεια 20 λεπτά.", {"R42", "R51.9"}),
    ("Επεισόδια υπέρτασης τη νύχτα. Διάρκεια 15 λεπτά.", {"I10"}),
    ("Γαστρίτιδα. Διάρκεια 15 λεπτά.", {"K29.70"}),
    ("Αϋπνία και ζάλη. Διάρκεια 20 λεπτά.", {"G47.00", "R42"}),
    ("Οίδημα σφυρών άμφω.", {"R60.0"}),          # out-of-surface → expected miss
]


def conformal_quantile_index(n: int, alpha: float) -> int:
    """k = ⌈(n+1)(1−α)⌉ — the order statistic carrying the finite-sample guarantee."""
    k = math.ceil((n + 1) * (1 - alpha))
    if k > n:
        raise ValueError(f"n={n} too small for α={alpha}: need n ≥ {k} calibration scores")
    return k


class ConformalReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    alpha: float
    n_calibration: int
    quantile_index: int
    q_hat: float
    floor: float                       # = 1 − q̂ : the calibrated evidence floor
    guarantee: str


class ConformalGate:
    """Fitted split-conformal gate. `fit` scores every calibration pair through the REAL
    pipeline (seal → extract → adversarial audit) and takes the exact conformal quantile."""

    def __init__(self, alpha: float, q_hat: float, n: int, k: int) -> None:
        self.alpha = alpha
        self.q_hat = q_hat
        self.n = n
        self.k = k
        self.floor = round(1.0 - q_hat, 4)

    # ---- scoring: what evidence did the pipeline give the TRUE codes? ----
    @staticmethod
    def _true_code_scores(core, note: str, true_codes: set[str]) -> list[float]:
        import asyncio
        st = asyncio.run(core.run_note(note))
        got = {p.code: p.score for p in st.proposals if p.kind in ("dx", "cpt")}
        return [got.get(code, 0.0) for code in sorted(true_codes)]

    @classmethod
    def fit(cls, core_factory, alpha: float = 0.10,
            calibration: Optional[list[tuple[str, set[str]]]] = None) -> "ConformalGate":
        """Default α=0.10: the calibration deliberately contains a ~5–8% out-of-lexicon miss
        tail, so a 5% guarantee is MATHEMATICALLY unattainable (q̂ would hit 1.0 and the floor
        would collapse to 0 — the formula telling the truth). 90% is the honest certificate the
        current coding surface supports; extending the lexicon tightens it, the math is fixed."""
        cal = calibration or CALIBRATION
        core = core_factory()
        scores: list[float] = []
        for note, truth in cal:
            scores.extend(cls._true_code_scores(core, note, truth))
        nonconformity = sorted(1.0 - s for s in scores)
        n = len(nonconformity)
        k = conformal_quantile_index(n, alpha)
        q_hat = nonconformity[k - 1]
        if q_hat >= 1.0:
            raise ValueError(
                f"α={alpha} is unattainable on this calibration set (q̂=1.0 — the miss tail "
                f"exceeds α). Increase α or extend the coding surface; refusing a vacuous gate.")
        return cls(alpha=alpha, q_hat=q_hat, n=n, k=k)

    def report(self) -> ConformalReport:
        return ConformalReport(
            alpha=self.alpha, n_calibration=self.n, quantile_index=self.k,
            q_hat=round(self.q_hat, 4), floor=self.floor,
            guarantee=(f"split conformal (Vovk et al. 2005): for an exchangeable new note, a TRUE "
                       f"code scores below the floor with probability ≤ {self.alpha:.0%} "
                       f"(q̂ = {self.q_hat:.2f} at order statistic {self.k}/{self.n})"))


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from omnios_core import OmniosCore
    gate = ConformalGate.fit(OmniosCore, alpha=0.10)
    r = gate.report()
    print(f"α={r.alpha} · n={r.n_calibration} pairs · k={r.quantile_index} · q̂={r.q_hat} → floor={r.floor}")
    print(r.guarantee)
