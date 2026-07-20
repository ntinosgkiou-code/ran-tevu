"""
test_auth.py — API-key store + scopes.
Run: python core/test_auth.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from auth import APIKeyStore


def test_issue_and_verify():
    s = APIKeyStore()
    k = s.issue("clinic-A", ["read", "write"])
    assert k.startswith("sk_") and s.verify(k)["tenant"] == "clinic-A"
    assert s.verify("sk_bogus") is None and s.verify(None) is None
    print("✅ issue/verify: valid key resolves to its tenant; unknown/None rejected")


def test_scopes():
    s = APIKeyStore()
    k = s.issue("clinic-A", ["ingest"])
    assert s.verify(k, scope="ingest") is not None
    assert s.verify(k, scope="admin") is None, "out-of-scope key must be rejected"
    kw = s.issue("clinic-B", ["*"])
    assert s.verify(kw, scope="anything") is not None, "wildcard scope allows all"
    print("✅ scopes: scoped keys enforced; wildcard '*' allows all")


def test_revoke():
    s = APIKeyStore()
    k = s.issue("clinic-A")
    s.revoke(k)
    assert s.verify(k) is None, "revoked key must stop working"
    print("✅ revoke: revoked key no longer verifies")


def main():
    test_issue_and_verify()
    test_scopes()
    test_revoke()
    print("\n🎯 API-KEY AUTH: ALL TESTS PASSED")


if __name__ == "__main__":
    main()
