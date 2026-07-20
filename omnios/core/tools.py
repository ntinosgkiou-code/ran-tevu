"""
tools.py — Governed tool-calling (MCP-style) + credential broker (Clawvisor pattern).
=====================================================================================
The YC-2026 "act inside other tools" layer: the agent acts in real systems (SMS, calendar, CRM, …)
but (a) NEVER sees raw credentials and (b) NEVER acts outside the ActionGovernor.

Each tool is registered as a governed Capability. The broker holds credentials in a vault and injects
them only at execution time (the agent passes just arguments). Every call flows through
permission → sandbox preview → approval → execute (creds injected) → audit → rollback.
Real MCP servers / SaaS APIs slot in behind the same `connector` signature — the governance
flow is unchanged. Deterministic, offline, Pydantic v2. Tests: python core/test_tools.py
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict

from governance import ActionGovernor, Capability, Proposal, Risk


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    description: str
    permission: str
    risk: Risk = Risk.HIGH
    input_schema: dict = {}          # MCP-style discovery: what args the tool takes


class CredentialVault:
    """Holds tool credentials. Internal only — a credential is NEVER returned to the caller/agent
    (Clawvisor: the middle layer sees the secret, the agent never does)."""

    def __init__(self) -> None:
        self._creds: dict[str, Any] = {}

    def store(self, tool: str, credential: Any) -> None:
        self._creds[tool] = credential

    def _inject(self, tool: str) -> Any:     # underscore = broker-internal
        return self._creds.get(tool)


# connector(credential, args) -> result   ·   undo(credential, args, result) -> None
Connector = Callable[[Any, dict], Any]
Undo = Callable[[Any, dict, Any], None]
Sandbox = Callable[[dict], dict]


class ToolBroker:
    def __init__(self, governor: ActionGovernor, vault: Optional[CredentialVault] = None) -> None:
        self.gov = governor
        self.vault = vault or CredentialVault()
        self._specs: dict[str, ToolSpec] = {}

    def register_tool(self, spec: ToolSpec, connector: Connector,
                      undo: Optional[Undo] = None, sandbox: Optional[Sandbox] = None) -> None:
        self._specs[spec.name] = spec

        def _sandbox(args: dict) -> dict:
            return sandbox(args) if sandbox else {"tool": spec.name, "args": args}

        def _execute(args: dict) -> Any:
            cred = self.vault._inject(spec.name)     # secret injected HERE, never exposed to the caller
            return connector(cred, args)

        _compensate = None
        if undo is not None:
            def _compensate(args: dict, result: Any) -> None:
                undo(self.vault._inject(spec.name), args, result)

        self.gov.register(Capability(
            name=f"tool:{spec.name}", execute=_execute, compensate=_compensate,
            risk=spec.risk, permission=spec.permission, sandbox=_sandbox,
        ))

    def call(self, tool: str, args: dict, reason: str = "") -> Proposal:
        """The agent's ONLY way to touch a tool — returns a governed Proposal (never the raw effect)."""
        if tool not in self._specs:
            raise KeyError(f"unknown tool {tool!r}")
        return self.gov.propose(f"tool:{tool}", args, reason=reason)

    def list_tools(self) -> list[dict]:
        return [s.model_dump() for s in self._specs.values()]


# ─────────────────────── mock connectors (real MCP/SaaS drop in behind these) ───────────────────────
def build_demo_broker() -> tuple[ToolBroker, dict]:
    """A broker with 3 governed tools + a shared mock backend, for demos/tests."""
    backend: dict[str, Any] = {"sms": [], "calendar": {}, "crm": {}, "_evt": 0}
    gov = ActionGovernor()
    broker = ToolBroker(gov)

    # SMS (Twilio/Yuboto) — credentials held by the broker, never seen by the agent
    broker.vault.store("sms_send", {"api_key": "SECRET_SMS_KEY"})
    def _sms(cred, a):
        assert cred and cred.get("api_key"), "connector must receive credentials from the vault"
        backend["sms"].append({"to": a["to"], "text": a["text"]})
        return {"provider_ref": f"SMS-{len(backend['sms'])}", "to": a["to"]}
    broker.register_tool(
        ToolSpec(name="sms_send", description="Send an SMS", permission="tool:sms",
                 input_schema={"to": "str", "text": "str"}),
        connector=_sms, undo=lambda cred, a, r: backend["sms"].remove({"to": a["to"], "text": a["text"]}),
        sandbox=lambda a: {"channel": "sms", "to": a["to"], "preview": a["text"][:40]})

    # Calendar (Google) — create event, rollback deletes it
    broker.vault.store("calendar_create", {"token": "SECRET_GCAL"})
    def _cal(cred, a):
        backend["_evt"] += 1
        eid = f"evt-{backend['_evt']}"
        backend["calendar"][eid] = {"title": a["title"], "slot": a["slot"]}
        return {"event_id": eid}
    broker.register_tool(
        ToolSpec(name="calendar_create", description="Create a calendar event", permission="tool:calendar",
                 input_schema={"title": "str", "slot": "str"}),
        connector=_cal, undo=lambda cred, a, r: backend["calendar"].pop(r["event_id"], None))

    # CRM upsert (revenue ops) — rollback restores the prior record
    broker.vault.store("crm_upsert", {"token": "SECRET_CRM"})
    def _crm(cred, a):
        prior = backend["crm"].get(a["contact"])
        backend["crm"][a["contact"]] = a["fields"]
        return {"contact": a["contact"], "prior": prior}
    broker.register_tool(
        ToolSpec(name="crm_upsert", description="Upsert a CRM contact", permission="tool:crm",
                 input_schema={"contact": "str", "fields": "dict"}),
        connector=_crm,
        undo=lambda cred, a, r: (backend["crm"].__setitem__(a["contact"], r["prior"]) if r["prior"]
                                 else backend["crm"].pop(a["contact"], None)))
    return broker, backend


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    broker, backend = build_demo_broker()
    print("tools (MCP-style discovery):", [t["name"] for t in broker.list_tools()])

    print("\n① agent calls sms_send WITHOUT permission:")
    p = broker.call("sms_send", {"to": "[TOK]", "text": "Ελάτε ξανά για το ραντεβού σας"}, reason="no-show recovery")
    print("  ", p.status.value, "—", p.why)

    print("② grant + call → sandbox preview + human approval:")
    broker.gov.grant("tool:sms")
    p = broker.call("sms_send", {"to": "[TOK]", "text": "Ελάτε ξανά για το ραντεβού σας"}, reason="no-show recovery")
    print("   evidence:", p.evidence, "| status:", p.status.value)
    ex = broker.gov.approve(p.id, approver="dr_papadopoulos")
    print("   executed:", ex.result, "| SMS backend:", backend["sms"])
    print("   ⟵ note: the agent never saw 'SECRET_SMS_KEY' — the broker injected it")

    print("③ rollback the sent action:")
    broker.gov.rollback(ex.execution_id, actor="dr_papadopoulos")
    print("   SMS backend after rollback:", backend["sms"])
