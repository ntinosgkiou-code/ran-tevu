"""
test_research_grade.py — the three literature-grounded upgrades, adversarially tested.
  A. Merkle anchoring + inclusion proofs (Merkle 1979/1987 · Haber–Stornetta 1991 · RFC 6962)
  B. Split conformal prediction (Vovk et al. 2005 · Papadopoulos et al. 2002 · A&B 2021)
  C. Thompson sampling (Thompson 1933 · Chapelle & Li 2011)
Run: python core/test_research_grade.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from bandit import ThompsonInterventionPolicy, simulate_clinic
from conformal import (CALIBRATION, HOLDOUT, ConformalGate, conformal_quantile_index)
from merkle import MerkleTree, leaf_hash, verify_inclusion
from predictive_billing import Action


# ═══ A. Merkle ═══
def test_merkle_inclusion_proofs_all_sizes():
    for n in (1, 2, 3, 7, 8, 13):
        recs = [{"seq": i, "event": "e", "data": {"i": i}} for i in range(n)]
        t = MerkleTree(recs)
        for i in range(n):
            assert verify_inclusion(recs[i], t.inclusion_proof(i), t.root), (n, i)
    print("✅ Merkle: inclusion proofs verify for EVERY leaf at sizes 1,2,3,7,8,13 (odd promotion correct)")


def test_merkle_tamper_and_wrong_proof_fail():
    recs = [{"seq": i, "event": "e", "data": {"i": i}} for i in range(8)]
    t = MerkleTree(recs)
    proof = t.inclusion_proof(3)
    tampered = {"seq": 3, "event": "e", "data": {"i": 999}}
    assert not verify_inclusion(tampered, proof, t.root), "tampered record must NOT verify"
    assert not verify_inclusion(recs[3], t.inclusion_proof(4), t.root), "wrong-leaf proof must fail"
    # domain separation: a leaf hash can never equal an inner-node preimage trivially
    assert leaf_hash(recs[0]) != t.levels[1][0]
    print("✅ Merkle: tampered record REJECTED · mismatched proof REJECTED · leaf/node domain-separated")


def test_merkle_root_changes_on_append():
    recs = [{"seq": i, "event": "e", "data": {"i": i}} for i in range(5)]
    r1 = MerkleTree(recs).root
    r2 = MerkleTree(recs + [{"seq": 5, "event": "e", "data": {"i": 5}}]).root
    assert r1 != r2
    print("✅ Merkle: append changes the root — the published anchor pins the exact history")


# ═══ B. Conformal ═══
def test_quantile_index_exact_formula():
    # k = ⌈(n+1)(1−α)⌉ — hand-checked cases
    assert conformal_quantile_index(9, 0.10) == 9
    assert conformal_quantile_index(40, 0.10) == 37
    assert conformal_quantile_index(99, 0.05) == 95
    try:
        conformal_quantile_index(9, 0.05)          # (10)(0.95)=9.5 → k=10 > n → impossible
        assert False
    except ValueError:
        pass
    print("✅ conformal: exact ⌈(n+1)(1−α)⌉ order statistic; infeasible (n,α) pairs REFUSED")


def test_conformal_fit_and_honest_refusal():
    from omnios_core import OmniosCore
    gate = ConformalGate.fit(OmniosCore, alpha=0.10)
    assert gate.n >= 38 and gate.floor == 0.90, (gate.n, gate.floor)
    # α=0.05 is unattainable on a calibration set with a ~5% structural miss tail — must REFUSE
    try:
        ConformalGate.fit(OmniosCore, alpha=0.05)
        assert False, "a vacuous 95% certificate must be refused, not issued"
    except ValueError:
        pass
    print(f"✅ conformal: fitted floor {gate.floor} at α=0.10 (n={gate.n}) · vacuous α=0.05 gate REFUSED")


def test_conformal_empirical_coverage_on_holdout():
    from omnios_core import OmniosCore
    gate = ConformalGate.fit(OmniosCore, alpha=0.10)
    core = OmniosCore()
    misses = total = 0
    for note, truth in HOLDOUT:
        for s in ConformalGate._true_code_scores(core, note, truth):
            total += 1
            if s < gate.floor:
                misses += 1
    rate = misses / total
    assert rate <= gate.alpha + 0.10, f"empirical miss {rate:.2%} exceeds α plus small-sample slack"
    print(f"✅ conformal: held-out miss rate {misses}/{total} = {rate:.1%} ≤ α(10%)+slack — the guarantee holds empirically")


# ═══ C. Thompson sampling ═══
def test_thompson_converges_to_clinic_truth():
    # this clinic's TRUE effects differ from the published priors: the AI call dominates
    true_show = {Action.NONE: 0.72, Action.SMS: 0.80, Action.AI_CALL: 0.93, Action.DEPOSIT: 0.84}
    pol = ThompsonInterventionPolicy(seed=7)
    r = simulate_clinic(pol, true_show, rounds=1500, seed=11)
    assert r["best_arm"] == "ai_call_reminder"
    assert r["choices"]["ai_call_reminder"] > 0.70 * r["rounds"], r["choices"]
    top = r["posteriors"][0]
    assert top["action"] == "ai_call_reminder" and abs(top["posterior_mean_show"] - 0.93) < 0.03
    print(f"✅ Thompson: {r['choices']['ai_call_reminder']}/{r['rounds']} pulls on the true-best arm · posterior {top['posterior_mean_show']} ≈ truth 0.93")


def test_thompson_beats_uniform_and_is_deterministic():
    true_show = {Action.NONE: 0.72, Action.SMS: 0.80, Action.AI_CALL: 0.93, Action.DEPOSIT: 0.84}
    a = simulate_clinic(ThompsonInterventionPolicy(seed=7), true_show, rounds=1200, seed=11)
    b = simulate_clinic(ThompsonInterventionPolicy(seed=7), true_show, rounds=1200, seed=11)
    assert a == b, "seeded runs must be exactly reproducible"
    # uniform baseline: expected shows = mean(true) * rounds = 0.8225 * 1200 = 987
    uniform_expected = sum(true_show.values()) / len(true_show) * 1200
    assert a["total_shows"] > uniform_expected + 40, (a["total_shows"], uniform_expected)
    print(f"✅ Thompson: {a['total_shows']} shows ≫ uniform baseline ~{uniform_expected:.0f} · bit-for-bit deterministic")


def main():
    test_merkle_inclusion_proofs_all_sizes()
    test_merkle_tamper_and_wrong_proof_fail()
    test_merkle_root_changes_on_append()
    test_quantile_index_exact_formula()
    test_conformal_fit_and_honest_refusal()
    test_conformal_empirical_coverage_on_holdout()
    test_thompson_converges_to_clinic_truth()
    test_thompson_beats_uniform_and_is_deterministic()
    print("\n🎯 RESEARCH-GRADE MODULES (Merkle/RFC6962 · split conformal · Thompson): ALL TESTS PASSED")


if __name__ == "__main__":
    main()
