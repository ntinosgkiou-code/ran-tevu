"""
fhir_adapter.py — claim manifest → HL7 FHIR R4 Bundle (interoperability layer).
================================================================================
Maps the OmniOS claim manifest onto official FHIR R4 resources so any EHR/payer system that
speaks FHIR can ingest our output directly:

  Bundle(collection) ─ Patient (pseudonymized — vault token only, GDPR/HIPAA minimum-necessary)
                     ├ Encounter (ambulatory, documented total time as an extension)
                     ├ Condition ×N (ICD-10-CM: http://hl7.org/fhir/sid/icd-10-cm)
                     ├ Procedure ×N (CPT: http://www.ama-assn.org/go/cpt, reasonReference → Condition)
                     └ Claim (professional; diagnosis[] + item[] sequences per the R4 schema)

Deterministic ids derived from the claim id (same claim → same Bundle). ZERO PII by
construction — the adapter only ever sees the already-tokenized claim; the caller re-verifies
with the vault's egress firewall. Tests: python core/test_fhir_adapter.py
"""
from __future__ import annotations

from typing import Any, Optional

ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10-cm"
CPT_SYSTEM = "http://www.ama-assn.org/go/cpt"
_ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
_CLAIM_TYPE = "http://terminology.hl7.org/CodeSystem/claim-type"


def _codeable(system: str, code: str, display: str = "") -> dict:
    coding: dict[str, Any] = {"system": system, "code": code}
    if display:
        coding["display"] = display
    return {"coding": [coding]}


def claim_to_fhir(claim: dict, patient_token: str = "patient-tokenized") -> dict:
    """OmniOS claim manifest → FHIR R4 Bundle (type: collection). Pure function, deterministic."""
    cid = claim["claim_id"]
    pid, eid = f"{cid}-patient", f"{cid}-encounter"

    patient = {"resourceType": "Patient", "id": pid,
               "identifier": [{"system": "urn:omnios:vault-token", "value": patient_token}],
               "meta": {"security": [{"system": "http://terminology.hl7.org/CodeSystem/v3-Confidentiality",
                                      "code": "R", "display": "restricted"}]}}

    encounter: dict[str, Any] = {
        "resourceType": "Encounter", "id": eid, "status": "finished",
        "class": {"system": _ACT_CODE, "code": "AMB", "display": "ambulatory"},
        "subject": {"reference": f"Patient/{pid}"}}
    minutes = claim.get("encounter", {}).get("total_minutes")
    if minutes is not None:
        encounter["extension"] = [{"url": "urn:omnios:total-encounter-minutes",
                                   "valueDecimal": float(minutes)}]

    conditions: list[dict] = []
    cond_ref: dict[str, str] = {}
    for i, d in enumerate(claim.get("diagnoses", []), start=1):
        rid = f"{cid}-cond-{i}"
        cond_ref[d["icd10"]] = rid
        conditions.append({
            "resourceType": "Condition", "id": rid,
            "clinicalStatus": _codeable("http://terminology.hl7.org/CodeSystem/condition-clinical",
                                        "active"),
            "code": _codeable(ICD10_SYSTEM, d["icd10"], d.get("label", "")),
            "subject": {"reference": f"Patient/{pid}"},
            "encounter": {"reference": f"Encounter/{eid}"},
            "evidence": ([{"detail": [{"display": d["evidence"]}]}] if d.get("evidence") else [])})

    procedures: list[dict] = []
    for i, p in enumerate(claim.get("procedures", []), start=1):
        reasons = [{"reference": f"Condition/{cond_ref[c]}"}
                   for c in p.get("linked_icd10", []) if c in cond_ref]
        procedures.append({
            "resourceType": "Procedure", "id": f"{cid}-proc-{i}", "status": "completed",
            "code": _codeable(CPT_SYSTEM, p["cpt"], p.get("label", "")),
            "subject": {"reference": f"Patient/{pid}"},
            "encounter": {"reference": f"Encounter/{eid}"},
            "reasonReference": reasons})                      # medical necessity, explicit in FHIR

    claim_items = [{"sequence": i,
                    "productOrService": _codeable(CPT_SYSTEM, p["cpt"], p.get("label", ""))}
                   for i, p in enumerate(claim.get("procedures", []), start=1)]
    em = claim.get("em")
    if em:
        claim_items.append({"sequence": len(claim_items) + 1,
                            "productOrService": _codeable(CPT_SYSTEM, em["code"],
                                                          em.get("band", "E/M visit"))})
    fhir_claim = {
        "resourceType": "Claim", "id": f"{cid}-claim", "status": "active", "use": "claim",
        "type": _codeable(_CLAIM_TYPE, "professional"),
        "patient": {"reference": f"Patient/{pid}"},
        "created-note": "timestamp assigned by the submitting system of record",
        "priority": _codeable("http://terminology.hl7.org/CodeSystem/processpriority", "normal"),
        "diagnosis": [{"sequence": i,
                       "diagnosisCodeableConcept": _codeable(ICD10_SYSTEM, d["icd10"],
                                                             d.get("label", ""))}
                      for i, d in enumerate(claim.get("diagnoses", []), start=1)],
        "item": claim_items,
        "extension": [{"url": "urn:omnios:confidence", "valueDecimal": claim.get("confidence", 0.0)},
                      {"url": "urn:omnios:risk-tier", "valueString": claim.get("tier", "claims")},
                      {"url": "urn:omnios:audit-seq",
                       "valueInteger": claim.get("integrity", {}).get("audit_seq", -1)}]}

    entries = [patient, encounter, *conditions, *procedures, fhir_claim]
    return {"resourceType": "Bundle", "id": f"{cid}-bundle", "type": "collection",
            "entry": [{"resource": r} for r in entries]}


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import asyncio
    import json
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from omnios_core import OmniosCore
    core = OmniosCore()
    st = asyncio.run(core.run_note(
        "Αρτηριακή υπέρταση. Έγινε ΗΚΓ. Διάρκεια 30 λεπτά."))
    bundle = claim_to_fhir(st.claim)
    kinds = [e["resource"]["resourceType"] for e in bundle["entry"]]
    print("Bundle:", bundle["id"], "| resources:", kinds)
    print(json.dumps(bundle["entry"][2]["resource"]["code"], ensure_ascii=False))
