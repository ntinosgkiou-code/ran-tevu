"""
crypto_at_rest.py — Encryption at rest για το OmniOS system-of-record (GDPR Art.9/32).
======================================================================================
Το KVStore (patients/appointments/invoices/notes) περνάει ΚΑΘΕ value μέσα από εδώ πριν
γραφτεί στη SQLite, ώστε ΑΜΚΑ/τηλέφωνα/ονόματα ασθενών να ΜΗΝ κάθονται ποτέ σε plaintext
στον δίσκο. Ίδιο, δοκιμασμένο pattern με το Ran-Tevu crypto_util.py:

  • Fernet (AES-128-CBC + HMAC-SHA256, authenticated encryption) μέσω `cryptography`.
  • Key ΜΟΝΟ από `RANTEVU_ENC_KEY` — ΠΟΤΕ από auth secrets (ADMIN_KEY/GATE_SECRET). Αν το
    key δενόταν σε rotating auth secret, μια rotation θα άχρηστευε ΟΛΑ τα δεδομένα (data loss).
  • Fail-closed: σε production (RAILWAY_* ή OMNIOS_REQUIRE_ENC=1) ΧΩΡΙΣ key → το app ΔΕΝ ξεκινά.
  • Rotation χωρίς data loss: παλιό key στο `RANTEVU_ENC_KEY_OLD` (decrypt-only fallback).
  • Backward-compatible: legacy plaintext (χωρίς prefix) διαβάζεται ως έχει → καμία migration
    δεν είναι υποχρεωτική· τα ήδη-γραμμένα demo rows συνεχίζουν να δουλεύουν.

Deterministic, stdlib + cryptography. Tests: python core/test_crypto_at_rest.py
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_PREFIX = "enc::v1::"
# "Production" = ο,τι δεν είναι το laptop σου: Railway env, ή ρητό opt-in.
_IS_PROD = bool(os.environ.get("RAILWAY_ENVIRONMENT")
                or os.environ.get("RAILWAY_PUBLIC_DOMAIN")
                or os.environ.get("OMNIOS_REQUIRE_ENC") == "1")


class CryptoConfigError(RuntimeError):
    """Raised at boot when production lacks a real encryption key (fail-closed)."""


class DecryptionError(RuntimeError):
    """Raised when an encrypted field cannot be decrypted (wrong key / corruption)."""


def _to_fernet_key(raw: str) -> bytes:
    """Accept a real 44-char Fernet key as-is, else derive a stable one via PBKDF2."""
    try:
        Fernet(raw.encode())
        return raw.encode()
    except Exception:
        dk = hashlib.pbkdf2_hmac("sha256", raw.encode(), b"omnios-clinic-v1", 200_000, dklen=32)
        return base64.urlsafe_b64encode(dk)


def _build_cipher() -> MultiFernet:
    keys = []
    primary = os.environ.get("RANTEVU_ENC_KEY")
    if primary:
        keys.append(Fernet(_to_fernet_key(primary)))
    old = os.environ.get("RANTEVU_ENC_KEY_OLD")   # decrypt-only fallback during rotation
    if old:
        keys.append(Fernet(_to_fernet_key(old)))
    if not keys:
        if _IS_PROD:
            raise CryptoConfigError(
                "RANTEVU_ENC_KEY is required in production — patient data must not be encrypted "
                "with a derived/insecure key. Set RANTEVU_ENC_KEY before deploying."
            )
        # Local dev only — NOT tied to any rotating auth secret.
        keys.append(Fernet(_to_fernet_key("omnios-LOCAL-DEV-ONLY-not-for-production")))
    return MultiFernet(keys)


_cipher = _build_cipher()


def encrypt(text):
    """Κρυπτογραφεί string. None/'' → ως έχει. Idempotent σε ήδη-κρυπτογραφημένα."""
    if text is None or text == "":
        return text
    if isinstance(text, str) and text.startswith(_PREFIX):
        return text
    return _PREFIX + _cipher.encrypt(str(text).encode("utf-8")).decode("ascii")


def decrypt(value):
    """Αποκρυπτογραφεί. Legacy plaintext (χωρίς prefix) → ως έχει. Σε αποτυχία
    αποκρυπτογράφησης κρυπτογραφημένου πεδίου: ΠΕΤΑΕΙ (prod) / marker (dev)."""
    if value is None or value == "":
        return value
    if not (isinstance(value, str) and value.startswith(_PREFIX)):
        return value  # legacy/demo plaintext
    try:
        return _cipher.decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, Exception) as exc:
        if _IS_PROD:
            raise DecryptionError(
                "stored field could not be decrypted (wrong key or data corruption)"
            ) from exc
        return "[decryption-failed]"


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)
