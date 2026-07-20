"""
auth.py — API-key authentication + scopes (production access control for the agent OS).
=======================================================================================
Once an agent OS ACTS, its endpoints must not be open. This is a minimal, dependency-injectable
API-key layer: keys are issued per tenant with scopes; a FastAPI dependency rejects missing/invalid
keys (401) and out-of-scope keys (403). Health stays public; every acting endpoint is secured.

Keys here are in-memory (demo); production persists them (KVStore) and rotates. Tests: python core/test_auth.py
"""
from __future__ import annotations

import secrets
from typing import Callable, Optional

from fastapi import Header, HTTPException


class APIKeyStore:
    def __init__(self) -> None:
        self._keys: dict[str, dict] = {}

    def issue(self, tenant: str, scopes: Optional[list[str]] = None) -> str:
        key = "sk_" + secrets.token_hex(16)
        self._keys[key] = {"tenant": tenant, "scopes": set(scopes or ["*"])}
        return key

    def register(self, key: str, tenant: str, scopes: Optional[list[str]] = None) -> None:
        """Register a KNOWN key value (e.g. the operator-provided OMNIOS_ADMIN_KEY from the
        environment) — production deployments bring their own secret instead of a random one."""
        if not key or len(key) < 12:
            raise ValueError("admin keys must be at least 12 characters")
        self._keys[key] = {"tenant": tenant, "scopes": set(scopes or ["*"])}

    def verify(self, key: Optional[str], scope: Optional[str] = None) -> Optional[dict]:
        rec = self._keys.get(key or "")
        if rec is None:
            return None
        if scope and "*" not in rec["scopes"] and scope not in rec["scopes"]:
            return None
        return rec

    def revoke(self, key: str) -> None:
        self._keys.pop(key, None)


def make_dependency(store: APIKeyStore, scope: Optional[str] = None) -> Callable:
    """Return a FastAPI dependency that enforces a valid API key (and optional scope)."""
    def _dep(x_api_key: Optional[str] = Header(default=None)) -> dict:
        rec = store.verify(x_api_key, scope=None)
        if rec is None:
            raise HTTPException(status_code=401, detail="invalid_or_missing_api_key")
        if scope and "*" not in rec["scopes"] and scope not in rec["scopes"]:
            raise HTTPException(status_code=403, detail=f"missing_scope:{scope}")
        return rec
    return _dep
