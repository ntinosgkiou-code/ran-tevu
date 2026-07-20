"""
crypto_util.py — Encryption at rest για ευαίσθητα ιατρικά δεδομένα (GDPR Art.9/32).

Κλινικά πεδία (διαγνώσεις, ιατρικές σημειώσεις, έγγραφα) αποθηκεύονται
ΚΡΥΠΤΟΓΡΑΦΗΜΕΝΑ στη SQLite με Fernet (AES-128, authenticated encryption).

ΑΣΦΑΛΕΙΑ (fail-closed):
  • Το key έρχεται ΜΟΝΟ από `RANTEVU_ENC_KEY` — ΠΟΤΕ από ADMIN_TOKEN/SECRET_KEY
    (αυτά είναι auth secrets που ροτάρονται· αν το key δενόταν σ' αυτά, μια
    rotation θα έκανε ΟΛΑ τα κλινικά δεδομένα μη-αποκρυπτογραφήσιμα = data loss).
  • Σε production (Railway) ΧΩΡΙΣ `RANTEVU_ENC_KEY` → το app ΔΕΝ ξεκινά (refuse,
    αντί να κρυπτογραφεί με insecure key).
  • Rotation χωρίς data loss: βάλε το παλιό key στο `RANTEVU_ENC_KEY_OLD` —
    αποκρυπτογραφεί και με τα δύο, κρυπτογραφεί πάντα με το νέο.
  • decrypt() σε αποτυχία: σε production ΠΕΤΑΕΙ error (ποτέ δεν δίνει ciphertext
    σαν κείμενο στον γιατρό, ποτέ δεν κρύβει wrong-key/corruption).

Backward-compatible: legacy plaintext (χωρίς prefix) επιστρέφεται ως έχει.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_PREFIX = "enc::v1::"
_IS_PROD = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PUBLIC_DOMAIN"))


class CryptoConfigError(RuntimeError):
    """Raised at boot when production lacks a real encryption key (fail-closed)."""


class DecryptionError(RuntimeError):
    """Raised when an encrypted clinical field cannot be decrypted (wrong key/corruption)."""


def _to_fernet_key(raw: str) -> bytes:
    """Accept a real 44-char Fernet key as-is, else derive a stable one via PBKDF2."""
    try:
        Fernet(raw.encode())
        return raw.encode()
    except Exception:
        dk = hashlib.pbkdf2_hmac("sha256", raw.encode(), b"rantevu-clinical-v1", 200_000, dklen=32)
        return base64.urlsafe_b64encode(dk)


def _build_cipher() -> MultiFernet:
    keys = []
    primary = os.environ.get("RANTEVU_ENC_KEY")
    if primary:
        keys.append(Fernet(_to_fernet_key(primary)))
    old = os.environ.get("RANTEVU_ENC_KEY_OLD")  # decrypt-only fallback during rotation
    if old:
        keys.append(Fernet(_to_fernet_key(old)))
    if not keys:
        if _IS_PROD:
            raise CryptoConfigError(
                "RANTEVU_ENC_KEY is required in production — clinical data must not be "
                "encrypted with a derived/insecure key. Set RANTEVU_ENC_KEY in Railway."
            )
        # Local dev only — NOT tied to any rotating auth secret.
        keys.append(Fernet(_to_fernet_key("rantevu-LOCAL-DEV-ONLY-not-for-production")))
        print("[crypto] ⚠️ DEV encryption key in use — never deploy without RANTEVU_ENC_KEY.")
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
                "clinical field could not be decrypted (wrong key or data corruption)"
            ) from exc
        return "[decryption-failed]"


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt_bytes(data: bytes) -> bytes:
    """Κρυπτογραφεί raw bytes (π.χ. ολόκληρο DB snapshot για off-site backup). Authenticated (Fernet)."""
    return _cipher.encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    """Αποκρυπτογραφεί ό,τι έφτιαξε το encrypt_bytes (για restore ενός backup)."""
    return _cipher.decrypt(token)


def rotate(value):
    """Re-encrypt a value under the primary key (for a documented rotation pass)."""
    return encrypt(decrypt(value))
