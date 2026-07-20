"""
debug_log.py — ακριβές, copy-paste-ready error logging για κάθε λειτουργία.
==========================================================================
Σκοπός: όταν κάτι σπάσει (Twilio/Resend/Anthropic/Stripe/Vapi/DB), να τυπώνεται το ΑΚΡΙΒΕΣ
σφάλμα — τύπος, μήνυμα, ΚΑΙ το HTTP response body του provider (εκεί κρύβεται το πραγματικό «γιατί»)
— με πρόθεμα το όνομα της function, ώστε να το κάνεις copy-paste στο Claude και να ξέρει ακριβώς.

Δύο τρόποι χρήσης:
  1) @logged  — decorator: πιάνει κάθε exception, τυπώνει πλήρες error + traceback, ΞΑΝΑ-πετάει.
  2) log_error("που", e)  — μέσα σε υπάρχον except block (όταν θες να επιστρέψεις safe default).
"""
from __future__ import annotations

import functools
import sys
import traceback


def _fmt_exc(e: BaseException) -> str:
    """Βγάζει ΟΛΗ τη χρήσιμη πληροφορία — και το κρυμμένο HTTP body των API providers."""
    parts = [f"{type(e).__name__}: {e}"]
    # urllib.error.HTTPError (Resend/IndexNow κ.λπ.): το πραγματικό μήνυμα είναι στο .read()
    if hasattr(e, "read") and hasattr(e, "code"):
        try:
            parts.append(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:1500]}")
        except Exception:
            pass
    # requests/httpx/stripe/anthropic κρύβουν το πραγματικό μήνυμα στο response body
    resp = getattr(e, "response", None)
    if resp is not None:
        status = getattr(resp, "status_code", getattr(resp, "status", "?"))
        try:
            body = resp.text if hasattr(resp, "text") else str(resp)
        except Exception:
            body = "<unreadable response>"
        parts.append(f"HTTP {status}: {str(body)[:1500]}")
    for attr in ("body", "message", "user_message", "detail"):
        v = getattr(e, attr, None)
        if v and str(v) not in parts[0]:
            parts.append(f"{attr}={str(v)[:800]}")
    return " | ".join(parts)


def log_error(where: str, e: BaseException, *, tb: bool = True) -> None:
    """Τύπωσε το ακριβές σφάλμα μέσα από ένα except block. Δεν πετάει — για safe-default paths."""
    print(f"\n[ERROR] {where}: {_fmt_exc(e)}", file=sys.stderr, flush=True)
    if tb:
        traceback.print_exc()


def logged(fn):
    """Decorator: τυπώνει το ΑΚΡΙΒΕΣ error (τύπος, μήνυμα, HTTP body, traceback) και ΞΑΝΑ-πετάει.
    Βάλ' το πάνω από functions που ΔΕΝ έχουν δικό τους try/except."""
    @functools.wraps(fn)
    def wrap(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:
            log_error(f"{getattr(fn, '__module__', '?')}.{fn.__name__}", e)
            raise
    return wrap
