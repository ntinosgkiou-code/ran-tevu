"""
config.py — environment-driven configuration (12-factor).
Defaults to in-memory (tests/dev); set env vars for a persistent, deployed product.
"""
import os

DB_PATH = os.environ.get("RANTEVU_DB", ":memory:")          # e.g. /data/rantevu.db on a volume
SAGA_DB = os.environ.get("RANTEVU_SAGA_DB", ":memory:")     # e.g. /data/saga.db
TICK_SECONDS = int(os.environ.get("RANTEVU_TICK_SECONDS", "30"))  # autonomous scan interval
PORT = int(os.environ.get("PORT", "8000"))

# ── Compliance profile (GDPR-first; HIPAA/dual as a switchable layer, never hardcoded) ──
COMPLIANCE_PROFILE = os.environ.get("RANTEVU_COMPLIANCE", "gdpr").lower()  # gdpr | hipaa | dual
ENC_KEY = os.environ.get("RANTEVU_ENC_KEY", "")   # 32-byte hex/base64 for PII-at-rest; ephemeral (dev) if unset


def compliance() -> dict:
    """Active compliance profile. GDPR is the default and the floor; if a US layer is
    enabled we take the STRICTER of the two on every axis (breach clock, consent, transfer)."""
    prof = {
        "name": "gdpr",
        "breach_notify_hours": 72,          # GDPR Art.33 controller→authority
        "explicit_consent_required": True,  # Art.9 special-category health data
        "require_encryption_at_rest": True, # Art.32
        "require_audit_log": True,
        "cross_border_transfer_guard": True,
        "baa_required": False,
    }
    if COMPLIANCE_PROFILE in ("hipaa", "dual"):
        prof["name"] = COMPLIANCE_PROFILE
        prof["breach_notify_hours"] = min(prof["breach_notify_hours"], 60)  # HIPAA 60d cap → keep the stricter 72h? no: pick lower clock
        prof["baa_required"] = True
    return prof
