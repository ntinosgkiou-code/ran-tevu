"""
test_store.py — persistence / system of record (survives restart).
Uses a real temp DB file and TWO separate connections to simulate a process restart.
Run: python core/test_store.py
"""
import json
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from store import KVStore, PersistentAuditLog
from agentic_loop import AgenticClinicAgent
from graph_rag import GraphRAG


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def _cleanup(path):
    for p in (path, path + "-wal", path + "-shm"):
        try:
            os.remove(p)
        except OSError:
            pass


def test_kvstore_survives_restart():
    path = _fresh_db()
    try:
        kv = KVStore(path)
        kv.put("ns", "greek", {"msg": "Ραντεβού αύριο", "n": 3})
        kv.close()
        kv2 = KVStore(path)                      # "restart": new connection to the same file
        assert kv2.get("ns", "greek") == {"msg": "Ραντεβού αύριο", "n": 3}, "value must survive restart"
        assert "greek" in kv2.all("ns")
        kv2.close()
        print("✅ KVStore: JSON (Greek) value persisted and reloaded after restart")
    finally:
        _cleanup(path)


def test_persistent_audit_verifies_across_restart_and_detects_tamper():
    path = _fresh_db()
    try:
        al = PersistentAuditLog(path)
        for e in ("gov.execute", "agent.update", "scribe.note_signed"):
            al.record(e, {"ok": True})
        al.close()
        al2 = PersistentAuditLog(path)           # restart
        assert len(al2) == 3 and al2.verify() is True, "chain must remain valid across restart"
        al2.record("gov.rollback", {"ok": True})  # append AFTER restart continues the chain
        assert al2.verify() is True and len(al2) == 4
        # tamper with a past record directly in the DB
        al2.con.execute("UPDATE audit SET data=? WHERE seq=0", (json.dumps({"ok": False}),))
        al2.con.commit()
        assert al2.verify() is False, "editing a past record must break the persisted chain"
        al2.close()
        print("✅ PersistentAuditLog: chain verifies across restart; a DB edit is detected")
    finally:
        _cleanup(path)


def test_agent_brain_remembers_learning():
    path = _fresh_db()
    try:
        kv = KVStore(path)
        a = AgenticClinicAgent()
        for i, showed in enumerate([True, False] * 20):   # true rate 0.5
            a.ingest({"type": "appointment", "id": f"B{i}", "value": 45})
            a.ingest({"type": "outcome", "id": f"B{i}", "showed": showed})
        learned = a.learner.no_show_prob()
        a.save(kv)
        kv.close()
        kv2 = KVStore(path)                       # restart
        b = AgenticClinicAgent().load(kv2)
        assert abs(b.learner.no_show_prob() - learned) < 1e-9, "learned no-show rate must survive restart"
        assert b.learner.n == a.learner.n
        kv2.close()
        print(f"✅ Agent brain: learned no-show rate {round(learned,3)} remembered after restart")
    finally:
        _cleanup(path)


def test_graphrag_snapshot_restore():
    KB = ("Η ασπιρίνη μειώνει τον πυρετό. Η ασπιρίνη αντενδείκνυται για έλκος. "
          "Το έλκος προκαλεί πόνο στο στομάχι.")
    g = GraphRAG()
    g.ingest_sync(KB)
    snap = g.snapshot()
    g2 = GraphRAG().restore(snap)                 # rebuilt from the persisted snapshot
    assert set(g2.graph.entities()) == set(g.graph.entities()), "KB entities must match after restore"
    r = g2.hybrid_search("ασπιρίνη πόνο στο στομάχι", hops=2)
    assert any("πόνο στο στομάχι" in p for p in r.graph_paths), "multi-hop reasoning must work on the restored KB"
    print(f"✅ GraphRAG: KB snapshot→restore keeps {len(g2.graph.entities())} entities + multi-hop reasoning")


def main():
    test_kvstore_survives_restart()
    test_persistent_audit_verifies_across_restart_and_detects_tamper()
    test_agent_brain_remembers_learning()
    test_graphrag_snapshot_restore()
    print("\n🎯 PERSISTENCE — system of record (survives restart): ALL TESTS PASSED")


if __name__ == "__main__":
    main()
