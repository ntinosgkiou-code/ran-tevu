"""
test_fhir_adapter.py — claim manifest → HL7 FHIR R4 Bundle.
Run: python core/test_fhir_adapter.py
"""
import asyncio
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fhir_adapter import CPT_SYSTEM, ICD10_SYSTEM, claim_to_fhir
from omnios_core import OmniosCore

NOTE = ("Ασθενής με ΑΜΚΑ 12036512345. Αρτηριακή υπέρταση. Έγινε ΗΚΓ. Διάρκεια 30 λεπτά.")


def _claim():
    core = OmniosCore()
    st = asyncio.run(core.run_note(NOTE))
    assert st.claim, "need a DONE claim to map"
    return core, st


def test_bundle_structure():
    _, st = _claim()
    b = claim_to_fhir(st.claim)
    kinds = [e["resource"]["resourceType"] for e in b["entry"]]
    assert b["resourceType"] == "Bundle" and b["type"] == "collection"
    assert kinds == ["Patient", "Encounter", "Condition", "Procedure", "Claim"]
    print(f"✅ Bundle(collection): {kinds} — the full R4 resource chain")


def test_official_code_systems_and_references():
    _, st = _claim()
    b = claim_to_fhir(st.claim)
    res = {e["resource"]["resourceType"]: e["resource"] for e in b["entry"]}
    cond, proc, clm = res["Condition"], res["Procedure"], res["Claim"]
    assert cond["code"]["coding"][0]["system"] == ICD10_SYSTEM
    assert cond["code"]["coding"][0]["code"] == "I10"
    assert proc["code"]["coding"][0]["system"] == CPT_SYSTEM
    assert proc["code"]["coding"][0]["code"] == "93000"
    # medical necessity is EXPLICIT in FHIR: the Procedure references the justifying Condition
    assert proc["reasonReference"][0]["reference"] == f"Condition/{cond['id']}"
    assert clm["diagnosis"][0]["sequence"] == 1 and len(clm["item"]) == 2   # CPT + E/M
    print("✅ official systems (icd-10-cm sid, AMA CPT) · Procedure.reasonReference → Condition (necessity)")


def test_zero_pii_and_determinism():
    core, st = _claim()
    b = claim_to_fhir(st.claim)
    payload = json.dumps(b, ensure_ascii=False)
    assert "12036512345" not in payload
    core.vault.assert_no_pii(payload)                        # the egress firewall approves FHIR too
    assert claim_to_fhir(st.claim) == b                      # same claim → identical Bundle
    print("✅ FHIR payload: ZERO PII (vault-verified) · deterministic (same claim → same Bundle)")


def test_confidence_and_tier_travel_with_the_claim():
    _, st = _claim()
    b = claim_to_fhir(st.claim)
    clm = next(e["resource"] for e in b["entry"] if e["resource"]["resourceType"] == "Claim")
    ext = {x["url"]: x for x in clm["extension"]}
    assert ext["urn:omnios:confidence"]["valueDecimal"] >= 0.96
    assert ext["urn:omnios:risk-tier"]["valueString"] == "claims"
    assert ext["urn:omnios:audit-seq"]["valueInteger"] >= 0
    print("✅ provenance travels: confidence + risk tier + audit-chain seq embedded as Claim extensions")


def main():
    test_bundle_structure()
    test_official_code_systems_and_references()
    test_zero_pii_and_determinism()
    test_confidence_and_tier_travel_with_the_claim()
    print("\n🎯 FHIR R4 ADAPTER — interoperable, PII-free, deterministic: ALL TESTS PASSED")


if __name__ == "__main__":
    main()
