"""admin_auth.py — shared constant-time ADMIN_TOKEN check for route blueprints.

app.py has its own inline require_master/_admin_ok (can't import from here into app.py
without a circular import — app.py is what registers every blueprint). This module exists
so per-blueprint files (voice_routes.py, inbox.py, ...) share ONE constant-time, fail-closed
implementation instead of each keeping a local `token != ADMIN_TOKEN` copy (timing-attackable,
and — with a blank ADMIN_TOKEN — `"" != ""` is False, i.e. it would ACCEPT a blank token).
"""
from functools import wraps
import hmac
import os

from flask import jsonify, request


def admin_ok(token: str) -> bool:
    """Constant-time master-admin check. Empty ADMIN_TOKEN → ALWAYS reject (fail-closed)."""
    expected = os.environ.get("ADMIN_TOKEN", "")
    return bool(expected) and hmac.compare_digest(token or "", expected)


def token_from_request() -> str:
    """Bearer header, falling back to ?token= query param (1-click/admin-UI links)."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return request.args.get("token", "").strip()


def require_master(fn):
    """Decorator form: 401 unless a valid ADMIN_TOKEN is presented (header or ?token=)."""
    @wraps(fn)
    def wrap(*args, **kwargs):
        if not admin_ok(token_from_request()):
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrap
