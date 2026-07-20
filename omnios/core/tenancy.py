"""
tenancy.py — Multi-tenant isolation (many clinics, one brain, zero cross-leak).
=============================================================================
A real product serves many clinics. Each Tenant gets its OWN isolated stack — knowledge base,
learner/ledger, permissions, and tool credentials — so clinic A can never see clinic B's data.
Each tenant persists under its own namespace (`tenant:<id>`) so the whole fleet survives restart.

Isolation is by construction: separate component instances per tenant, no shared mutable state.
(The audit chain stays a single tamper-evident org log; sensitive data never enters it.)
Deterministic, offline, Pydantic-free surface. Tests: python core/test_tenancy.py
"""
from __future__ import annotations

from typing import Optional

from audit import AUDIT
from agentic_loop import AgenticClinicAgent, OutcomeLearner
from governance import ActionGovernor
from pipeline import MedicalIntelligencePipeline
from tools import ToolBroker


class Tenant:
    """One clinic's fully-isolated stack."""

    def __init__(self, tenant_id: str, permissions: Optional[list[str]] = None) -> None:
        self.id = tenant_id
        self.pipeline = MedicalIntelligencePipeline()      # isolated KB + PII vault
        self.agent = AgenticClinicAgent()                  # isolated learner + ledger
        self.governor = ActionGovernor(permissions=permissions)   # isolated permissions
        self.broker = ToolBroker(self.governor)            # isolated tool credentials

    # convenience passthroughs
    def ingest(self, text: str, source: Optional[str] = None):
        return self.pipeline.ingest_sync(text, source)

    async def ask(self, query: str):
        return await self.pipeline.ask(query)

    def entities(self) -> list[str]:
        return self.pipeline.graph_rag.graph.entities()

    # persistence — this tenant only, under its own namespace
    def _ns(self) -> str:
        return f"tenant:{self.id}"

    def save(self, store) -> None:
        ns = self._ns()
        store.put(ns, "kb", self.pipeline.graph_rag.snapshot())
        store.put(ns, "agent", {"learner": self.agent.learner.to_dict(),
                                "recovered_eur": self.agent.recovered_eur, "appts": self.agent._appts})
        store.put(ns, "grants", {"permissions": sorted(self.governor._granted)})

    def load(self, store) -> "Tenant":
        ns = self._ns()
        kb = store.get(ns, "kb")
        if kb:
            self.pipeline.graph_rag.restore(kb)
        ag = store.get(ns, "agent")
        if ag:
            self.agent.learner = OutcomeLearner.from_dict(ag["learner"])
            self.agent.recovered_eur = ag["recovered_eur"]
            self.agent._appts = ag.get("appts", {})
        gr = store.get(ns, "grants")
        if gr:
            for p in gr["permissions"]:
                self.governor.grant(p)
        return self


class TenantManager:
    def __init__(self) -> None:
        self._tenants: dict[str, Tenant] = {}

    def create(self, tenant_id: str, permissions: Optional[list[str]] = None) -> Tenant:
        if tenant_id in self._tenants:
            raise ValueError(f"tenant {tenant_id!r} already exists")
        t = Tenant(tenant_id, permissions)
        self._tenants[tenant_id] = t
        AUDIT.record("tenant.create", {"tenant": tenant_id})
        return t

    def get(self, tenant_id: str) -> Tenant:
        if tenant_id not in self._tenants:
            raise KeyError(f"unknown tenant {tenant_id!r}")   # never silently cross tenants
        return self._tenants[tenant_id]

    def ids(self) -> list[str]:
        return list(self._tenants)

    def save_all(self, store) -> None:
        for t in self._tenants.values():
            t.save(store)
        store.put("tenants", "index", {"ids": self.ids()})

    def load_all(self, store) -> "TenantManager":
        idx = store.get("tenants", "index") or {"ids": []}
        for tid in idx["ids"]:
            self._tenants[tid] = Tenant(tid).load(store)
        return self


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    tm = TenantManager()
    a = tm.create("physio-reth", permissions=["tool:sms"])
    b = tm.create("dental-athens")
    a.ingest("Η φυσικοθεραπεία μειώνει την οσφυαλγία. Η κρυοθεραπεία μειώνει το οίδημα.")
    b.ingest("Το σφράγισμα θεραπεύει την τερηδόνα. Η ζάχαρη προκαλεί τερηδόνα.")
    print("A entities:", a.entities())
    print("B entities:", b.entities())
    print("isolated (no overlap):", set(a.entities()).isdisjoint(b.entities()))
    print("A has tool:sms grant:", "tool:sms" in a.governor._granted,
          "| B has it:", "tool:sms" in b.governor._granted)
