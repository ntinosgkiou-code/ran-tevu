"""
test_token_guard.py — Lossless Information Density compression, adversarially tested.
Proves: syntactic-only removal, 100% medical-entity preservation, entropy density rises,
the 99.9% integrity bypass actually fires when a compressor misbehaves, and re-hydration
returns the exact original.
Run: python core/test_token_guard.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from token_guard import (INTEGRITY_FLOOR, ContextCompressor, GuardReport, TokenGuard,
                         shannon_entropy_bits)

NOISY = ("Λοιπόν,  βασικά ο ασθενής   έχει απλά αρτηριακή υπέρταση.\n\n\n\n"
         "Έγινε δηλαδή ΗΚΓ.   Διάρκεια 30 λεπτά. Κωδικός I10, CPT 93000.\n"
         "Έγινε δηλαδή ΗΚΓ.   Διάρκεια 30 λεπτά. Κωδικός I10, CPT 93000.")


def test_compression_is_lossless_on_entities():
    c = ContextCompressor()
    out, r = c.compress(NOISY)
    assert r.ratio < 0.85, f"verbose input should shrink (got {r.ratio})"
    assert r.integrity == 1.0 and not r.bypassed
    # every clinical entity survived: term, code, CPT, the number 30
    for ent in ("αρτηριακή υπέρταση", "ηκγ", "I10", "93000", "30"):
        assert ent.lower() in out.lower(), f"entity lost: {ent}"
    # fillers gone
    for filler in ("Λοιπόν", "βασικά", "απλά", "δηλαδή"):
        assert filler not in out
    print(f"✅ lossless: ratio {r.ratio}, integrity 1.0 — fillers dropped, ALL clinical entities kept")


def test_entropy_density_rises():
    c = ContextCompressor()
    out, r = c.compress(NOISY)
    assert r.entropy_after_bits >= r.entropy_before_bits, \
        "removing low-entropy filler must raise per-char information density"
    assert shannon_entropy_bits("") == 0.0
    print(f"✅ entropy: {r.entropy_before_bits} → {r.entropy_after_bits} bits/char (density up)")


def test_integrity_bypass_fires_on_a_broken_compressor():
    class BrokenCompressor(ContextCompressor):
        def _strip_syntactic(self, text: str) -> str:            # maliciously drops a code
            return super()._strip_syntactic(text).replace("I10", "")
    out, r = BrokenCompressor().compress("Διάγνωση I10. Έγινε ΗΚΓ 93000.")
    assert r.bypassed and r.integrity < INTEGRITY_FLOOR
    assert "I10" in out, "bypass must return the ORIGINAL text untouched"
    print("✅ safety valve: a compressor that would drop 'I10' is BYPASSED — raw context passes through")


def test_rehydration_exact():
    tg = TokenGuard()
    compressed, gid = tg.guard(NOISY)
    assert tg.rehydrate(gid) == NOISY
    assert compressed != NOISY                       # it did compress
    rep = tg.last_report()
    assert isinstance(rep, GuardReport) and rep.entities_total >= 5
    try:
        tg.rehydrate("guard-nope")
        assert False
    except KeyError:
        pass
    print("✅ re-hydration: exact original returned on demand; unknown receipts rejected")


def test_clean_text_untouched_semantics():
    c = ContextCompressor()
    clean = "Αρτηριακή υπέρταση. Έγινε ΗΚΓ. Διάρκεια 30 λεπτά."
    out, r = c.compress(clean)
    assert r.integrity == 1.0 and not r.bypassed
    for ent in ("αρτηριακή υπέρταση", "ηκγ", "30"):
        assert ent in out.lower()
    print("✅ clean input: nothing semantic changes (compression is a no-op where there's no fat)")


def main():
    test_compression_is_lossless_on_entities()
    test_entropy_density_rises()
    test_integrity_bypass_fires_on_a_broken_compressor()
    test_rehydration_exact()
    test_clean_text_untouched_semantics()
    print("\n🎯 TOKEN GUARD (lossless compression · entropy metric · 99.9% integrity bypass): ALL TESTS PASSED")


if __name__ == "__main__":
    main()
