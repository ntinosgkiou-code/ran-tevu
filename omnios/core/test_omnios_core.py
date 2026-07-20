"""
test_omnios_core.py — the Mythos-class compliance state machine, adversarially tested.
Proves: manifest validity, PII-zero egress, correct coding on the happy path, adversarial
auditor refutation (medical necessity), the 0.96 hard gate + HUMAN_OVERRIDE resume,
E/M-time consistency, determinism, and traceable value math.
Run: python core/test_omnios_core.py
"""
import asyncio
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from audit import AUDIT
from omnios_core import (MANIFEST, OmniosCore, Phase, load_manifest, wilson_lower_bound)

CLEAN_NOTE = ("Ασθενής με ΑΜΚΑ 12036512345, τηλ 6945123456. Αρτηριακή υπέρταση, ρυθμισμένη. "
              "Έγινε ΗΚΓ ρουτίνας. Διάρκεια 30 λεπτά.")


def run(coro):
    return asyncio.run(coro)


def test_manifest_valid():
    m = load_manifest()
    assert m["guardrails"]["confidence_floor"] == 0.96
    assert {"ICD10-FORMAT", "CPT-FORMAT", "MEDICAL-NECESSITY", "DX-REQUIRED",
            "EM-TIME-CONSISTENCY", "PII-EGRESS-ZERO"} <= {r["id"] for r in m["coding_rules"]}
    print("✅ manifest: loads, regexes compile, 0.96 claims-tier floor, all core rules present")


def test_happy_path_codes_and_gate():
    core = OmniosCore()
    st = run(core.run_note(CLEAN_NOTE, patient_type="established"))
    assert st.phase is Phase.DONE and not st.frozen
    assert st.confidence >= 0.96
    dx = [d["icd10"] for d in st.claim["diagnoses"]]
    cpt = [p["cpt"] for p in st.claim["procedures"]]
    assert dx == ["I10"] and cpt == ["93000"] and st.claim["em"]["code"] == "99214"
    assert st.claim["procedures"][0]["linked_icd10"] == ["I10"], "necessity linkage must be explicit"
    assert st.trail == ["EXTRACT→AUDIT", "AUDIT→GATE", "GATE→FORMAT", "FORMAT→DONE"]
    print(f"✅ happy path: I10 + 93000(→I10) + 99214 (30' established), conf {st.confidence} ≥ 0.96 → DONE")


def test_pii_never_leaves():
    core = OmniosCore()
    st = run(core.run_note(CLEAN_NOTE))
    payload = json.dumps(st.claim, ensure_ascii=False)
    assert "12036512345" not in payload and "6945123456" not in payload
    assert "[REDACTED_AMKA_1]" in st.note_redacted and "[REDACTED_PHONE_1]" in st.note_redacted
    core.vault.assert_no_pii(payload)                       # egress firewall passes
    # and the raw PII never landed in the audit chain either
    chain = json.dumps(AUDIT.tail(50), ensure_ascii=False)
    assert "12036512345" not in chain
    print("✅ PII: tokenized at the edge, ZERO in the claim payload and ZERO in the audit chain")


def test_adversarial_auditor_refutes_unjustified_procedure():
    core = OmniosCore()
    # pharyngitis + fever do NOT justify an ECG (justified_by: I/R07/R06/E11) → necessity reject
    st = run(core.run_note("Φαρυγγίτιδα με πυρετό. Έγινε ΗΚΓ. Διάρκεια 20 λεπτά."))
    assert st.phase is Phase.HUMAN_OVERRIDE and st.frozen
    assert "MEDICAL-NECESSITY" in st.freeze_reason
    refuted = [v for v in st.verdicts if not v.confirmed]
    assert any(v.rule_id == "MEDICAL-NECESSITY" and v.code == "93000" for v in refuted)
    print("✅ adversarial auditor: ECG without a justifying dx REFUTED → FROZEN (necessity rule)")


def test_low_confidence_freezes_then_human_resumes():
    core = OmniosCore()
    st = run(core.run_note("Ασθενής αναφέρει αστάθεια στον διαβήτη του."))   # stem hit → 0.90
    assert st.phase is Phase.HUMAN_OVERRIDE and st.frozen
    assert "confidence 0.90 < 0.96" in st.freeze_reason
    assert core.pending_overrides() and core.pending_overrides()[0].id == st.id
    # clinician approves → graph resumes at FORMAT → DONE, with who recorded
    done = run(core.approve(st.id, "dr_kardia"))
    assert done.phase is Phase.DONE and done.claim["actor"] == "dr_kardia"
    assert not core.pending_overrides()
    assert AUDIT.verify(), "audit chain must stay intact through freeze + override"
    print("✅ hard gate: stem-evidence 0.90 < 0.96 → FROZEN → clinician approve → DONE (who audited)")


def test_reject_path():
    core = OmniosCore()
    st = run(core.run_note("Ασθενής αναφέρει αστάθεια στον διαβήτη του."))
    assert st.phase is Phase.HUMAN_OVERRIDE
    out = core.reject(st.id, "dr_kardia", reason="needs an in-person exam first")
    assert out.phase is Phase.REJECTED and out.claim is None
    print("✅ override desk: clinician can REJECT — nothing is formatted, decision audited")


def test_no_dx_freezes():
    core = OmniosCore()
    st = run(core.run_note("Ο ασθενής προσήλθε για πιστοποιητικό. Διάρκεια 15 λεπτά."))
    assert st.phase is Phase.HUMAN_OVERRIDE and "DX-REQUIRED" in st.freeze_reason
    print("✅ presence rule: zero billable diagnoses → DX-REQUIRED freeze (no empty claims)")


def test_em_time_bands():
    core = OmniosCore()
    st = run(core.run_note("Αρτηριακή υπέρταση. Διάρκεια 25 λεπτά.", patient_type="established"))
    assert st.claim["em"]["code"] == "99213", st.claim["em"]
    st2 = run(core.run_note("Αρτηριακή υπέρταση. Διάρκεια 50 λεπτά.", patient_type="new"))
    assert st2.claim["em"]["code"] == "99204"
    print("✅ E/M: 25' established → 99213 · 50' new → 99204 (CPT 2021 time bands)")


def test_determinism():
    a = run(OmniosCore().run_note(CLEAN_NOTE))
    b = run(OmniosCore().run_note(CLEAN_NOTE))
    strip = lambda c: {k: v for k, v in c.items() if k not in ("claim_id", "integrity")}
    assert strip(a.claim) == strip(b.claim) and a.confidence == b.confidence
    print("✅ determinism: identical note → identical codes, confidence, and rule checks")


def test_value_tracker_math():
    core = OmniosCore()
    st = run(core.run_note(CLEAN_NOTE))
    v = st.value
    assert v["time_saved_min"] > 0 and v["net_saved_eur"] > 0
    assert v["gross_value_eur"] == round(v["time_saved_min"] / 60.0 * 18.0, 4)
    t = core.tracker.totals()
    assert t["fee_eur"] == round(0.10 * t["net_saved_eur"], 2)
    print(f"✅ value: {v['time_saved_min']}' saved → €{v['net_saved_eur']} net (traceable) · fee = 10% = €{t['fee_eur']}")


def test_wilson_bound_sane():
    assert wilson_lower_bound(0, 0) == 0.0
    assert 0.55 < wilson_lower_bound(10, 10) < 0.75      # small-n honesty: never inflated
    assert wilson_lower_bound(95, 100) > 0.88
    print("✅ statistics: Wilson 95% lower bound is conservative for small n, tight for large n")


def test_risk_tiers_clinical_docs_stricter():
    """A mixed-evidence note (exact dx 0.99 + stem dx 0.90 → mean 0.965) passes the 0.96 claims
    tier but FREEZES at the 0.978 clinical-docs tier — one engine, risk-scaled strictness."""
    mixed = ("Αρτηριακή υπέρταση. Έγινε ΗΚΓ. "
             "Ασθενής αναφέρει αστάθεια στον διαβήτη του. Διάρκεια 30 λεπτά.")
    core = OmniosCore()
    ok = run(core.run_note(mixed, tier="claims"))
    assert ok.phase is Phase.DONE and 0.96 <= ok.confidence < 0.978, ok.confidence
    strict = run(core.run_note(mixed, tier="clinical_docs"))
    assert strict.phase is Phase.HUMAN_OVERRIDE and strict.frozen
    assert "clinical_docs" in strict.freeze_reason
    # and a fully-exact note (0.9867) clears BOTH tiers — the gate measures evidence, not vibes
    top = run(core.run_note(CLEAN_NOTE, tier="clinical_docs"))
    assert top.phase is Phase.DONE
    try:
        run(core.run_note(CLEAN_NOTE, tier="nonexistent"))
        assert False
    except ValueError:
        pass
    print(f"✅ risk tiers: conf {ok.confidence} → claims PASS / clinical_docs FREEZE · 0.9867 clears both")


def test_causal_evidence_in_claim():
    core = OmniosCore()
    st = run(core.run_note(CLEAN_NOTE))
    assert st.causal and st.causal[0]["cpt"] == "93000"
    assert st.causal[0]["path"] == ["I10", "cardiovascular_strain", "93000"]
    assert st.claim["causal_evidence"][0]["chain"] == "I10 → cardiovascular_strain → 93000"
    print(f"✅ causal auditor: claim carries the explicit chain «{st.causal[0]['chain']}»")


def test_context_rehydration_recovers_lost_info():
    """A (deliberately broken) guard drops the diagnosis → the gate would freeze DX-REQUIRED →
    Context Re-Hydration pulls the FULL raw text back → the deep cycle re-runs → DONE."""
    from token_guard import TokenGuard

    class LossyGuard(TokenGuard):
        def guard(self, text: str):
            compressed, gid = super().guard(text)
            return compressed.replace("Αρτηριακή υπέρταση, ρυθμισμένη.", ""), gid

    core = OmniosCore(token_guard=LossyGuard())
    st = run(core.run_note(CLEAN_NOTE, patient_type="established"))
    assert st.rehydrated is True, "the re-hydration event must have fired"
    assert "AUDIT→GATE" in st.trail and "GATE→EXTRACT" in st.trail, st.trail
    assert st.phase is Phase.DONE and st.claim["diagnoses"][0]["icd10"] == "I10"
    # and honest scoping: substantive rejections (necessity) are NOT rescued by re-hydration
    core2 = OmniosCore()
    bad = run(core2.run_note("Φαρυγγίτιδα με πυρετό. Έγινε ΗΚΓ. Διάρκεια 20 λεπτά."))
    assert bad.phase is Phase.HUMAN_OVERRIDE and not bad.rehydrated
    print("✅ re-hydration: lost dx recovered from FULL raw context → DONE · necessity rejects still freeze")


def test_api_wiring_one_system():
    """The state machine is LIVE in the product API and feeds the same Explanation log the UI shows."""
    from fastapi.testclient import TestClient
    from api import app, _DEV_API_KEY
    key = TestClient(app, headers={"X-API-Key": _DEV_API_KEY})
    anon = TestClient(app)
    assert anon.post("/compliance/run", json={"transcript": "x"}).status_code == 401
    exps_before = len(key.get("/explanations").json()["entries"])
    r = key.post("/compliance/run", json={"transcript": CLEAN_NOTE, "patient_type": "established"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["phase"] == "DONE" and b["claim"]["diagnoses"][0]["icd10"] == "I10"
    # a frozen run lands in the SAME Explanation log the «Αποφάσεις» tab renders (one system)
    fr = key.post("/compliance/run", json={"transcript": "Ασθενής αναφέρει αστάθεια στον διαβήτη του."}).json()
    assert fr["phase"] == "HUMAN_OVERRIDE"
    ov = key.post(f"/compliance/override/{fr['id']}", json={"clinician_id": "dr_kardia"})
    assert ov.status_code == 200 and ov.json()["phase"] == "DONE"
    exps_after = len(key.get("/explanations").json()["entries"])
    assert exps_after >= exps_before + 2
    pend = key.get("/compliance/pending").json()
    assert pend["value"]["executions"] >= 2
    # FHIR R4 export of a DONE claim, over HTTP (keyed read-only, PII-free by construction)
    fh = key.get(f"/compliance/claim/{b['id']}/fhir")
    assert fh.status_code == 200 and fh.json()["resourceType"] == "Bundle"
    kinds = [e["resource"]["resourceType"] for e in fh.json()["entry"]]
    assert "Claim" in kinds and "Condition" in kinds
    # tiers over HTTP: unknown tier → 422; clinical_docs accepted
    assert key.post("/compliance/run", json={"transcript": CLEAN_NOTE, "tier": "bogus"}).status_code == 422
    strict = key.post("/compliance/run", json={"transcript": CLEAN_NOTE, "tier": "clinical_docs"}).json()
    assert strict["tier"] == "clinical_docs"
    print("✅ API: secured run · override→DONE · FHIR Bundle over HTTP · tiers validated (422 on bogus)")


def main():
    test_manifest_valid()
    test_happy_path_codes_and_gate()
    test_pii_never_leaves()
    test_adversarial_auditor_refutes_unjustified_procedure()
    test_low_confidence_freezes_then_human_resumes()
    test_reject_path()
    test_no_dx_freezes()
    test_em_time_bands()
    test_determinism()
    test_value_tracker_math()
    test_wilson_bound_sane()
    test_risk_tiers_clinical_docs_stricter()
    test_causal_evidence_in_claim()
    test_context_rehydration_recovers_lost_info()
    test_api_wiring_one_system()
    print("\n🎯 OMNIOS CORE (Extractor→Auditor→Gate→Formatter, 0.96 tier): ALL TESTS PASSED")


if __name__ == "__main__":
    main()
