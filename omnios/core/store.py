"""
store.py — Persistent system of record (the company brain that survives a restart).
===================================================================================
The YC-2026 pattern demands a durable "system of records": what was ingested, decided, and done
must outlive the process. This provides two SQLite-backed primitives on `config.DB_PATH`:

  • KVStore            — namespaced JSON documents (component state: learner, ledger, KB snapshots).
  • PersistentAuditLog — the tamper-evident hash chain (EU AI Act Art.12), persisted and
                         re-verifiable across restarts (uses a STABLE secret from env, not per-process).

Set `RANTEVU_DB=/data/brain.db` (a volume) and `RANTEVU_AUDIT_SECRET=<32+ bytes>` in production.
In dev both default to in-memory / a fixed dev secret. Deterministic, stdlib-only, typed.
Tests: python core/test_store.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
from typing import Any, Optional

import config

# Encryption at rest for PII values (GDPR Art.32). CryptoConfigError (prod without RANTEVU_ENC_KEY)
# propagates → fail-closed boot. ImportError (cryptography absent) degrades to plaintext in dev only;
# production always ships cryptography via requirements, so the fail-closed guard still applies there —
# but if a broken deploy ever DOES ship without the `cryptography` package, this must not silently
# start writing patient PII as plaintext. Same "production" detection as crypto_at_rest.py itself
# (duplicated, not imported: the whole point is this branch runs when that module failed to import).
_IS_PROD = bool(os.environ.get("RAILWAY_ENVIRONMENT")
                or os.environ.get("RAILWAY_PUBLIC_DOMAIN")
                or os.environ.get("OMNIOS_REQUIRE_ENC") == "1")
try:
    from crypto_at_rest import encrypt as _enc, decrypt as _dec
except ImportError:
    if _IS_PROD:
        raise RuntimeError(
            "PII encryption at rest is unavailable ('cryptography' package failed to import) "
            "while running in production (RAILWAY_* set). Refusing to boot and silently store "
            "patient PII as plaintext (GDPR Art.32). Fix the deploy's requirements.txt.")
    def _enc(x):  # type: ignore[misc]
        return x
    def _dec(x):  # type: ignore[misc]
        return x

_GENESIS = "0" * 64
# STABLE across restarts (unlike audit.py's per-process secret) so a persisted chain stays verifiable.
_AUDIT_SECRET = os.environ.get("RANTEVU_AUDIT_SECRET", "rantevu-audit-dev-secret").encode("utf-8")


def _connect(path: Optional[str] = None) -> sqlite3.Connection:
    p = path or config.DB_PATH
    # Create the parent dir if it doesn't exist (e.g. OMNIOS_CLINIC_DB=/data/clinic.db on a fresh
    # container with no volume mounted yet) — otherwise sqlite raises "unable to open database file".
    if p != ":memory:":
        _dir = os.path.dirname(p)
        if _dir:
            try:
                os.makedirs(_dir, exist_ok=True)
            except OSError:
                pass
    # check_same_thread=False: FastAPI runs sync endpoints across a threadpool, so one connection is
    # touched by many threads. We serialize every access with a lock (below), which keeps it safe.
    con = sqlite3.connect(p, check_same_thread=False)
    if p != ":memory:":
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")   # multi-tenant: several stores share one file —
    return con                                    # wait for the writer instead of 'database is locked'


class KVStore:
    """Namespaced JSON key-value store. Survives restart when `path` is a real file.
    Thread-safe: every operation holds a lock, so it's usable from a web threadpool."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.con = _connect(path)
        self._lock = threading.Lock()
        with self._lock:
            self.con.execute("CREATE TABLE IF NOT EXISTS kv "
                             "(ns TEXT, key TEXT, value TEXT, updated_at TEXT, PRIMARY KEY(ns,key))")
            self.con.commit()

    def put(self, ns: str, key: str, value: dict) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO kv(ns,key,value,updated_at) VALUES(?,?,?,datetime('now')) "
                "ON CONFLICT(ns,key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (ns, key, _enc(json.dumps(value, ensure_ascii=False))))
            self.con.commit()

    def get(self, ns: str, key: str) -> Optional[dict]:
        with self._lock:
            row = self.con.execute("SELECT value FROM kv WHERE ns=? AND key=?", (ns, key)).fetchone()
        return json.loads(_dec(row[0])) if row else None

    def all(self, ns: str) -> dict[str, dict]:
        # ORDER BY rowid → deterministic insertion order (FIFO), so e.g. waitlist backfill is fair.
        with self._lock:
            rows = self.con.execute("SELECT key,value FROM kv WHERE ns=? ORDER BY rowid", (ns,)).fetchall()
        return {k: json.loads(_dec(v)) for k, v in rows}

    def delete(self, ns: str, key: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM kv WHERE ns=? AND key=?", (ns, key))
            self.con.commit()

    def close(self) -> None:
        with self._lock:
            self.con.close()


class PersistentAuditLog:
    """SQLite tamper-evident hash chain. Reopen the same DB and `verify()` still holds; any row
    edited/inserted/deleted breaks the chain."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.con = _connect(path)
        self._lock = threading.Lock()
        with self._lock:
            self.con.execute("CREATE TABLE IF NOT EXISTS audit "
                             "(seq INTEGER PRIMARY KEY, event TEXT, data TEXT, prev_hash TEXT, this_hash TEXT)")
            self.con.commit()

    def _hash(self, prev_hash: str, payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hmac.new(_AUDIT_SECRET, (prev_hash + canonical).encode("utf-8"), hashlib.sha256).hexdigest()

    def _last(self) -> tuple[int, str]:
        row = self.con.execute("SELECT seq, this_hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        return (row[0], row[1]) if row else (-1, _GENESIS)

    def record(self, event: str, data: dict) -> int:
        with self._lock:                # serialized: chain append must be atomic across threads
            last_seq, prev = self._last()
            seq = last_seq + 1
            payload = {"seq": seq, "event": event, "data": data}
            this_hash = self._hash(prev, payload)
            self.con.execute("INSERT INTO audit(seq,event,data,prev_hash,this_hash) VALUES(?,?,?,?,?)",
                             (seq, event, json.dumps(data, ensure_ascii=False), prev, this_hash))
            self.con.commit()
        return seq

    def verify(self) -> bool:
        with self._lock:
            rows = self.con.execute(
                "SELECT seq,event,data,prev_hash,this_hash FROM audit ORDER BY seq").fetchall()
        prev = _GENESIS
        for seq, event, data_json, prev_hash, this_hash in rows:
            if prev_hash != prev:
                return False
            expected = self._hash(prev, {"seq": seq, "event": event, "data": json.loads(data_json)})
            if expected != this_hash:
                return False
            prev = this_hash
        return True

    def tail(self, n: int = 20) -> list[dict]:
        with self._lock:
            rows = self.con.execute("SELECT seq,event,data FROM audit ORDER BY seq DESC LIMIT ?",
                                    (n,)).fetchall()
        return [{"seq": s, "event": e, "data": json.loads(d)} for s, e, d in reversed(rows)]

    def __len__(self) -> int:
        with self._lock:
            return self.con.execute("SELECT COUNT(*) FROM audit").fetchone()[0]

    def close(self) -> None:
        self.con.close()


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    import tempfile
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # write with one "process"
        kv = KVStore(path); kv.put("agent", "learner", {"alpha": 22.7, "beta": 27.3, "n": 40}); kv.close()
        al = PersistentAuditLog(path)
        for e in ("gov.execute", "agent.update", "scribe.note_signed"):
            al.record(e, {"ok": True});
        al.close()
        # reopen with a fresh "process" — the brain remembers
        kv2 = KVStore(path); print("reloaded learner:", kv2.get("agent", "learner")); kv2.close()
        al2 = PersistentAuditLog(path)
        print(f"audit records after restart: {len(al2)} · chain intact: {al2.verify()}")
        al2.record("gov.rollback", {"ok": True})
        print("after appending post-restart · chain intact:", al2.verify())
        al2.close()
    finally:
        os.remove(path)
