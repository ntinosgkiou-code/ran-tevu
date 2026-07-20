"""
durable_saga.py — Durable Saga with crash-recovery (Garcia-Molina & Salem, 1987).
=================================================================================
Each step is persisted before/after its effect:  started → done; the saga ends
committed or compensated. If the process dies mid-saga, the log keeps it in
`running`; a new executor's `recover()` sweep compensates every completed step in
reverse, restoring a consistent state. Backing store is pluggable (':memory:' for
the live app, a file in the recovery test to prove cross-restart durability).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Tuple


class CrashSignal(Exception):
    """Simulates sudden process death — the saga is left `running` (no compensation)."""


class ConflictSignal(Exception):
    """Business no-op: the step could not commit (e.g. slot taken). No compensation."""
    def __init__(self, incumbent_ref: str):
        self.incumbent_ref = incumbent_ref
        super().__init__(incumbent_ref)


# registry entry: {"do": fn(payload)->effect_dict, "undo": fn(effect_dict)->None}
Registry = Dict[str, Dict[str, Callable]]


class SagaExecutor:
    def __init__(self, db_path: str, registry: Registry) -> None:
        self.registry = registry
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        cur = self._conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS sagas(saga_id TEXT PRIMARY KEY, state TEXT, ts REAL)")
        cur.execute("CREATE TABLE IF NOT EXISTS saga_steps("
                    "saga_id TEXT, idx INTEGER, name TEXT, status TEXT, payload TEXT, ts REAL)")
        self._conn.commit()

    def _exec(self, sql: str, args: Tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, args)
            self._conn.commit()

    def begin(self) -> str:
        sid = uuid.uuid4().hex[:12]
        self._exec("INSERT INTO sagas VALUES(?,?,?)", (sid, "running", time.time()))
        return sid

    def run(self, sid: str, steps: List[Tuple[str, Dict[str, Any]]]) -> Dict[str, Any]:
        done: List[Tuple[int, str, Dict[str, Any]]] = []
        for idx, (name, payload) in enumerate(steps):
            self._exec("INSERT INTO saga_steps VALUES(?,?,?,?,?,?)",
                       (sid, idx, name, "started", json.dumps(payload, ensure_ascii=False), time.time()))
            try:
                effect = self.registry[name]["do"](payload)
            except ConflictSignal as exc:
                self._exec("UPDATE sagas SET state='compensated' WHERE saga_id=?", (sid,))
                return {"ok": False, "conflict": True, "incumbent_ref": exc.incumbent_ref}
            except CrashSignal:
                raise  # leave saga 'running' → recovery will compensate `done` steps
            except Exception as exc:
                self._compensate(sid, done)
                self._exec("UPDATE sagas SET state='compensated' WHERE saga_id=?", (sid,))
                return {"ok": False, "failed": name, "error": str(exc)}
            effect = effect if isinstance(effect, dict) else payload
            self._exec("UPDATE saga_steps SET status='done', payload=? WHERE saga_id=? AND idx=?",
                       (json.dumps(effect, ensure_ascii=False), sid, idx))
            done.append((idx, name, effect))
        self._exec("UPDATE sagas SET state='committed' WHERE saga_id=?", (sid,))
        return {"ok": True, "results": [d[2] for d in done]}

    def _compensate(self, sid: str, done: List[Tuple[int, str, Dict[str, Any]]]) -> None:
        for idx, name, effect in reversed(done):
            try:
                self.registry[name]["undo"](effect)
            finally:
                self._exec("UPDATE saga_steps SET status='compensated' WHERE saga_id=? AND idx=?", (sid, idx))

    def recover(self) -> List[str]:
        """Compensate every saga left `running` (i.e. interrupted by a crash)."""
        with self._lock:
            running = [r[0] for r in self._conn.execute(
                "SELECT saga_id FROM sagas WHERE state='running'").fetchall()]
        recovered: List[str] = []
        for sid in running:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT idx,name,status,payload FROM saga_steps WHERE saga_id=? ORDER BY idx DESC",
                    (sid,)).fetchall()
            done = [(idx, name, json.loads(payload)) for idx, name, status, payload in rows if status == "done"]
            print(f"[SAGA RECOVERY] saga {sid}: compensating {len(done)} committed step(s)")
            self._compensate(sid, done)
            self._exec("UPDATE sagas SET state='compensated' WHERE saga_id=?", (sid,))
            recovered.append(sid)
        return recovered

    def stats(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT state, COUNT(*) FROM sagas GROUP BY state").fetchall()
        return {state: n for state, n in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
