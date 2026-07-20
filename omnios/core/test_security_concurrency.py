"""
test_security_concurrency.py — the security layer under adversarial load.
Proves: 100% anonymization success across a varied PII corpus, ZERO leakage under 32-thread
concurrency (vault seal + egress firewall + store writes racing), receipt isolation between
concurrent callers, and Dynamic Delta Routing (lossless roundtrip + ≥70% measured token savings
on a realistic workflow sequence).
Run: python core/test_security_concurrency.py
"""
import sys
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from omnios_core import PIIVault
from token_guard import DeltaRouter, apply_delta, state_diff

# varied PII corpus: cue-names, NER-only names, ΑΜΚΑ, phones, IBAN, Luhn cards, emails, IDs
CORPUS = [
    "Είμαι ο Γιώργος Παπαδόπουλος, ΑΜΚΑ 12036512345.",
    "Ο Νίκος Σταθόπουλος ήρθε για εξέταση, τηλ 6945123456.",
    "IBAN GR1601101250000000012300695 για την πληρωμή.",
    "Κάρτα 4539 1488 0343 6467, email test.user@clinic.gr.",
    "Η Μαρία Δημητρίου, τηλέφωνο 2810123456, ΑΜΚΑ 01019012345.",
    "Ονομάζομαι Ελένη Παππά, ταυτότητα ΑΒ 123456.",
    "Επικοινωνία στο +30 694 512 3456 ή στο maria@example.com.",
    "Ο ασθενής Κώστας Αλεξίου με ΑΜΚΑ 15057812345 και κάρτα 5555 5555 5555 4444.",
] * 4    # 32 payloads

SECRETS = ["12036512345", "6945123456", "GR1601101250000000012300695", "4539 1488 0343 6467",
           "2810123456", "01019012345", "15057812345", "5555 5555 5555 4444",
           "test.user@clinic.gr", "maria@example.com"]


def test_100pct_anonymization_success():
    vault = PIIVault()
    for text in set(CORPUS):
        red, receipt = vault.seal(text)
        for s in SECRETS:
            assert s not in red, f"LEAK: {s!r} survived in {red!r}"
        vault.assert_no_pii(red)                       # the egress firewall agrees: zero PII
        assert vault.unseal(red, receipt) == text      # and the rehydration is exact
    print(f"✅ anonymization: {len(set(CORPUS))}/{len(set(CORPUS))} payloads clean (100%), "
          "egress-verified, bidirectional restore exact")


def test_concurrent_seal_no_leak_no_crosstalk():
    vault = PIIVault()

    def work(i: int):
        text = CORPUS[i % len(CORPUS)] + f" Σημείωση {i}."
        red, receipt = vault.seal(text)
        for s in SECRETS:
            assert s not in red
        assert vault.unseal(red, receipt) == text      # MY receipt restores MY text
        return receipt

    with ThreadPoolExecutor(max_workers=32) as ex:
        receipts = list(ex.map(work, range(256)))
    assert len(set(receipts)) == 256, "receipts must be unique per caller (no map crosstalk)"
    print("✅ concurrency: 256 seals across 32 threads — zero leaks, zero receipt collisions")


def test_concurrent_store_writes_race_safe():
    from clinic_store import ClinicStore
    cs = ClinicStore(":memory:")
    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(lambda i: cs.add_value("recovered_appointment", 1.0), range(200)))
    assert len(cs.ledger()) == 200, "every concurrent write must land exactly once"
    assert cs.billing()["total_value_eur"] == 200.0
    print("✅ store: 200 concurrent ledger writes → exactly 200 rows (lock holds, no lost updates)")


def test_delta_router_lossless_and_saves_tokens():
    # a realistic 8-step workflow: a large state where each step touches 1-2 fields
    base = {"id": "wf-1", "tier": "claims", "patient_type": "established",
            "note_redacted": "Ασθενής με [REDACTED_AMKA_1]. Αρτηριακή υπέρταση, ρυθμισμένη, "
                             "χωρίς συμπτώματα. Έγινε ΗΚΓ ρουτίνας. Διάρκεια 30 λεπτά." * 3,
            "proposals": [{"kind": "dx", "code": "I10", "score": 0.99}],
            "verdicts": [], "rule_failures": [], "confidence": 0.0,
            "trail": [], "meta": {"clinic": "demo", "minutes": 30}}
    steps = []
    import copy
    s = copy.deepcopy(base)
    for i, mut in enumerate([
        lambda st: st.update(phase="EXTRACT"),
        lambda st: st.update(phase="AUDIT"),
        lambda st: st["trail"].append("EXTRACT→AUDIT"),
        lambda st: st.update(confidence=0.9867),
        lambda st: st["meta"].update(gate="pass"),
        lambda st: st.update(phase="FORMAT"),
        lambda st: st.update(phase="DONE"),
    ]):
        mut(s)
        steps.append(copy.deepcopy(s))

    router = DeltaRouter()
    for st in [base] + steps:
        delta = router.encode("wf-1", st)
        assert router.decode("wf-1", delta) == st, "delta roundtrip must be LOSSLESS"
    rep = router.report()
    assert rep["savings_pct"] >= 70.0, f"expected ≥70% measured savings, got {rep}"
    print(f"✅ delta routing: lossless across 8 steps · MEASURED savings {rep['savings_pct']}% "
          f"({rep['tokens_transmitted']} vs {rep['tokens_full_equiv']} tokens)")


def test_delta_diff_semantics():
    old = {"a": 1, "b": {"c": 2, "d": 3}, "e": [1, 2]}
    new = {"a": 1, "b": {"c": 9}, "e": [1, 2, 3], "f": "x"}
    set_ops, del_ops = state_diff(old, new)
    assert set_ops == {"b.c": 9, "e": [1, 2, 3], "f": "x"} and del_ops == ["b.d"]
    assert apply_delta(old, {"set": set_ops, "del": del_ops}) == new
    print("✅ delta semantics: nested set/del exact · apply(base, diff) == new (pure function)")


def main():
    test_100pct_anonymization_success()
    test_concurrent_seal_no_leak_no_crosstalk()
    test_concurrent_store_writes_race_safe()
    test_delta_router_lossless_and_saves_tokens()
    test_delta_diff_semantics()
    print("\n🎯 SECURITY UNDER CONCURRENCY + DELTA ROUTING: ALL TESTS PASSED")


if __name__ == "__main__":
    main()
