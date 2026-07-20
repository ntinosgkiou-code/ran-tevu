"""
test_tenancy.py — multi-tenant isolation (many clinics, zero cross-leak, per-tenant persistence).
Run: python core/test_tenancy.py
"""
import asyncio
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from tenancy import TenantManager
from store import KVStore

A_KB = "Η φυσικοθεραπεία μειώνει την οσφυαλγία. Η κρυοθεραπεία μειώνει το οίδημα."
B_KB = "Το σφράγισμα θεραπεύει την τερηδόνα. Η ζάχαρη προκαλεί τερηδόνα."


def _mgr():
    tm = TenantManager()
    a = tm.create("physio-reth", permissions=["tool:sms"])
    b = tm.create("dental-athens")
    a.ingest(A_KB)
    b.ingest(B_KB)
    return tm, a, b


def test_knowledge_isolated():
    _, a, b = _mgr()
    assert set(a.entities()).isdisjoint(b.entities()), "clinics must not share KB entities"
    assert "τερηδόνα" not in a.entities() and "οσφυαλγία" not in b.entities(), "no cross-leak"
    print(f"✅ KB isolation: A={len(a.entities())} entities, B={len(b.entities())}, zero overlap")


def test_answer_only_from_own_kb():
    _, a, b = _mgr()
    ra = asyncio.run(a.ask("φυσικοθεραπεία οσφυαλγία"))
    assert "οσφυαλγία" in ra.answer and "τερηδόνα" not in ra.answer, "A must answer from A's KB only"
    rb = asyncio.run(b.ask("οσφυαλγία"))     # B has no such fact
    assert "οσφυαλγία" not in rb.answer, "B must not answer A's fact"
    print("✅ query isolation: each clinic answers only from its own knowledge")


def test_learner_and_permissions_isolated():
    _, a, b = _mgr()
    for i, showed in enumerate([True, False, True, False]):
        a.agent.ingest({"type": "appointment", "id": f"A{i}", "value": 45})
        a.agent.ingest({"type": "outcome", "id": f"A{i}", "showed": showed})
    assert a.agent.learner.n == 4 and b.agent.learner.n == 0, "learners must be independent"
    assert "tool:sms" in a.governor._granted and "tool:sms" not in b.governor._granted, "permissions per tenant"
    print("✅ learner + permissions isolation: A learned/permitted, B untouched")


def test_get_unknown_tenant_raises():
    tm, _, _ = _mgr()
    try:
        tm.get("ghost")
        assert False, "unknown tenant must raise (never silently cross tenants)"
    except KeyError:
        pass
    print("✅ safety: unknown tenant id raises, never crosses tenants")


def test_fleet_persists_per_tenant():
    path = None
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        tm, a, b = _mgr()
        for i in range(3):
            a.agent.ingest({"type": "appointment", "id": f"A{i}", "value": 45})
            a.agent.ingest({"type": "outcome", "id": f"A{i}", "showed": True})
        a_prob = a.agent.learner.no_show_prob()
        kv = KVStore(path); tm.save_all(kv); kv.close()
        # "restart": fresh manager reloads the whole fleet
        kv2 = KVStore(path)
        tm2 = TenantManager().load_all(kv2)
        assert set(tm2.ids()) == {"physio-reth", "dental-athens"}
        a2, b2 = tm2.get("physio-reth"), tm2.get("dental-athens")
        assert "οσφυαλγία" in a2.entities() and "τερηδόνα" in b2.entities(), "each KB restored to its own tenant"
        assert set(a2.entities()).isdisjoint(b2.entities()), "isolation preserved across restart"
        assert abs(a2.agent.learner.no_show_prob() - a_prob) < 1e-9, "A's learning restored"
        assert "tool:sms" in a2.governor._granted and "tool:sms" not in b2.governor._granted
        kv2.close()
        print("✅ fleet persistence: both clinics reloaded under their own namespace, isolation intact")
    finally:
        for p in (path, path + "-wal", path + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass


def main():
    test_knowledge_isolated()
    test_answer_only_from_own_kb()
    test_learner_and_permissions_isolated()
    test_get_unknown_tenant_raises()
    test_fleet_persists_per_tenant()
    print("\n🎯 MULTI-TENANT ISOLATION (many clinics, zero cross-leak): ALL TESTS PASSED")


if __name__ == "__main__":
    main()
