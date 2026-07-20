"""
omnios_manifest_loader.py — single, validated source of the clinical manifest.
==============================================================================
Extracted into its own module so every subsystem (state machine, token guard, causal graph,
FHIR adapter) imports ONE validated manifest with no circular dependencies. Fails loudly at
import time if the manifest is structurally invalid — a compliance engine must refuse to boot
on a bad rulebook, not degrade silently.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


class ManifestError(ValueError):
    """The clinical manifest is structurally invalid — refuse to start."""


_MANIFEST_PATH = Path(__file__).with_name("clinical_manifest.json")


def load_manifest(path: Path = _MANIFEST_PATH) -> dict:
    m = json.loads(path.read_text(encoding="utf-8"))
    required = ("workflow", "entities", "icd10_lexicon", "icd10_stems", "cpt_rules",
                "em_table", "coding_rules", "guardrails", "billing")
    for key in required:
        if key not in m:
            raise ManifestError(f"manifest missing required section: {key}")
    for rule in m["coding_rules"]:
        if rule.get("type") == "format":
            re.compile(rule["pattern"])                     # must compile or we refuse to boot
        if rule.get("severity") not in ("reject", "high_risk", "warn"):
            raise ManifestError(f"rule {rule.get('id')}: bad severity")
    for fam, bands in m["em_table"].items():
        if fam.startswith("_"):
            continue
        for code, lo, hi in bands:
            if not (isinstance(lo, int) and isinstance(hi, int) and lo < hi):
                raise ManifestError(f"em_table band invalid: {code}")
    floor = m["guardrails"]["confidence_floor"]
    if not 0.5 <= floor <= 1.0:
        raise ManifestError("confidence_floor out of range")
    for tier, val in m["guardrails"].get("tiers", {}).items():
        if not 0.5 <= float(val) <= 1.0:
            raise ManifestError(f"tier {tier} floor out of range")
    return m


MANIFEST = load_manifest()
