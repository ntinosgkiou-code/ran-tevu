"""
governance.py — Action Governance / Trust Layer.
================================================
The YC-2026 pattern (373-startup analysis): the winners don't generate text — they ACT inside real
systems. But "a bad action can cost money and trust", so a guardrail layer is REQUIRED before an
agent may touch anything: **permissions · sandbox (dry-run) · evals · approvals · audit · rollback**,
and every change must show *what changed, why, what evidence, and who approved it*.

This generalizes the HITL + saga-compensation + tamper-evident audit we already have into ONE
broker that EVERY capability registers with — nothing acts outside it (Clawvisor-style). It turns
the system from "an AI feature" into an auditable **system of record for decisions**.

Flow:  propose(cap, params)
         → PERMISSION check  → (deny)
         → SANDBOX dry-run   → evidence/preview (no commit)
         → EVAL gate         → (block)
         → risk? HIGH ⇒ PENDING_APPROVAL ;  LOW ⇒ auto-execute
       approve(id, who) → EXECUTE → audit(what/why/evidence/who) → rollback token
       rollback(exec_id) → compensate → audit

Deterministic, offline, Pydantic v2, typed. Tests: python core/test_governance.py
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict

from audit import AUDIT


class Risk(str, Enum):
    LOW = "low"
    HIGH = "high"


class Status(str, Enum):
    DENIED = "DENIED"                       # blocked by permissions
    BLOCKED = "BLOCKED"                     # blocked by an eval gate
    PENDING_APPROVAL = "PENDING_APPROVAL"   # high-risk, awaiting a human
    EXECUTED = "EXECUTED"                   # done (auto low-risk, or after approval)


class Capability:
    """A registered action. `sandbox` previews the effect WITHOUT committing (evidence);
    `execute` commits; `compensate` undoes it (rollback); `eval_gate` may veto before execution."""

    def __init__(self, name: str, execute: Callable[[dict], Any],
                 compensate: Optional[Callable[[dict, Any], None]] = None,
                 risk: Risk = Risk.HIGH, permission: Optional[str] = None,
                 sandbox: Optional[Callable[[dict], dict]] = None,
                 eval_gate: Optional[Callable[[dict], bool]] = None) -> None:
        self.name = name
        self.execute = execute
        self.compensate = compensate
        self.risk = risk
        self.permission = permission
        self.sandbox = sandbox
        self.eval_gate = eval_gate


class Proposal(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    capability: str
    risk: Risk
    status: Status
    evidence: dict            # the sandbox preview — what WOULD change
    reason: str
    why: str = ""             # governor's note (denied/blocked reason)
    execution_id: Optional[str] = None
    result: Optional[Any] = None


class Execution(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    capability: str
    approved_by: str
    result: Any = None
    rolled_back: bool = False


class ActionGovernor:
    def __init__(self, permissions: Optional[list[str]] = None) -> None:
        self._caps: dict[str, Capability] = {}
        self._granted: set[str] = set(permissions or [])
        self._proposals: dict[str, dict] = {}
        self._executions: dict[str, dict] = {}
        self._seq = 0

    # ---- setup ----
    def register(self, cap: Capability) -> None:
        self._caps[cap.name] = cap

    def grant(self, permission: str) -> None:
        self._granted.add(permission)

    # ---- preview (read-only: permission → sandbox → eval → routing, NEVER executes) ----
    def preview(self, name: str, params: dict) -> dict:
        """Safe for open/UI use: shows what WOULD happen (permission verdict, sandbox evidence,
        risk routing) without committing anything. The execute path stays behind approve()."""
        cap = self._caps.get(name)
        if cap is None:
            raise KeyError(f"unknown capability {name!r}")
        if cap.permission and cap.permission not in self._granted:
            return {"status": "DENIED", "risk": cap.risk.value, "permission": cap.permission,
                    "evidence": {}, "why": f"missing permission '{cap.permission}'"}
        evidence = cap.sandbox(params) if cap.sandbox else {"note": "no sandbox preview available"}
        if cap.eval_gate and not cap.eval_gate(params):
            return {"status": "BLOCKED", "risk": cap.risk.value, "permission": cap.permission,
                    "evidence": evidence, "why": "failed eval gate"}
        return {"status": "PREVIEW", "risk": cap.risk.value, "permission": cap.permission,
                "would_route_to": ("EXECUTED (auto · low-risk)" if cap.risk is Risk.LOW
                                   else "PENDING_APPROVAL (human)"),
                "evidence": evidence, "why": ""}

    # ---- propose (context → decision, guarded) ----
    def propose(self, name: str, params: dict, reason: str = "") -> Proposal:
        cap = self._caps.get(name)
        if cap is None:
            raise KeyError(f"unknown capability {name!r}")
        self._seq += 1
        pid = f"prop-{self._seq:04d}"

        # 1) PERMISSION
        if cap.permission and cap.permission not in self._granted:
            AUDIT.record("gov.denied", {"id": pid, "cap": name, "need": cap.permission, "why": "not_permitted"})
            return Proposal(id=pid, capability=name, risk=cap.risk, status=Status.DENIED,
                            evidence={}, reason=reason, why=f"missing permission '{cap.permission}'")

        # 2) SANDBOX dry-run → evidence (no commit)
        evidence = cap.sandbox(params) if cap.sandbox else {"note": "no sandbox preview available"}

        # 3) EVAL gate
        if cap.eval_gate and not cap.eval_gate(params):
            AUDIT.record("gov.blocked", {"id": pid, "cap": name, "why": "eval_failed", "evidence": evidence})
            return Proposal(id=pid, capability=name, risk=cap.risk, status=Status.BLOCKED,
                            evidence=evidence, reason=reason, why="failed eval gate")

        # 4) RISK → approval routing
        self._proposals[pid] = {"cap": name, "params": params, "evidence": evidence, "reason": reason}
        AUDIT.record("gov.propose", {"id": pid, "cap": name, "risk": cap.risk.value, "evidence": evidence})
        if cap.risk is Risk.LOW:
            return self._execute(pid, approver="system(auto:low-risk)")
        return Proposal(id=pid, capability=name, risk=cap.risk, status=Status.PENDING_APPROVAL,
                        evidence=evidence, reason=reason, why="high-risk — human approval required")

    # ---- approve / reject ----
    def approve(self, proposal_id: str, approver: str) -> Proposal:
        if not approver or not approver.strip():
            raise PermissionError("an approver is required")
        if proposal_id not in self._proposals:
            raise KeyError(f"no pending proposal {proposal_id!r}")
        return self._execute(proposal_id, approver)

    def reject(self, proposal_id: str, approver: str, reason: str = "") -> None:
        self._proposals.pop(proposal_id, None)
        AUDIT.record("gov.reject", {"id": proposal_id, "by": approver, "reason": reason})

    # ---- execute (commit + audit what/why/evidence/who) ----
    def _execute(self, proposal_id: str, approver: str) -> Proposal:
        rec = self._proposals.pop(proposal_id)
        cap = self._caps[rec["cap"]]
        result = cap.execute(rec["params"])
        self._seq += 1
        eid = f"exec-{self._seq:04d}"
        self._executions[eid] = {"cap": cap.name, "params": rec["params"], "result": result,
                                 "compensate": cap.compensate, "rolled_back": False}
        AUDIT.record("gov.execute", {"exec": eid, "cap": cap.name, "approved_by": approver,
                                     "reason": rec["reason"], "evidence": rec["evidence"]})
        return Proposal(id=proposal_id, capability=cap.name, risk=cap.risk, status=Status.EXECUTED,
                        evidence=rec["evidence"], reason=rec["reason"],
                        execution_id=eid, result=result)

    # ---- rollback ----
    def rollback(self, execution_id: str, actor: str) -> bool:
        ex = self._executions.get(execution_id)
        if ex is None:
            raise KeyError(f"no execution {execution_id!r}")
        if ex["rolled_back"]:
            return False
        if ex["compensate"] is None:
            raise ValueError(f"capability {ex['cap']!r} has no rollback")
        ex["compensate"](ex["params"], ex["result"])
        ex["rolled_back"] = True
        AUDIT.record("gov.rollback", {"exec": execution_id, "cap": ex["cap"], "by": actor})
        return True


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    outbox: list[dict] = []
    gov = ActionGovernor(permissions=["ledger:read"])   # note: messaging NOT granted

    gov.register(Capability(
        name="send_recovery_sms", risk=Risk.HIGH, permission="messaging:send",
        sandbox=lambda p: {"to": p["to_token"], "preview": f"Θα σταλεί SMS: {p['text'][:40]}…"},
        execute=lambda p: outbox.append({"to": p["to_token"], "text": p["text"]}) or {"sent_to": p["to_token"]},
        compensate=lambda p, r: outbox.remove({"to": p["to_token"], "text": p["text"]}),
    ))

    print("① unpermitted action:")
    d = gov.propose("send_recovery_sms", {"to_token": "[TOKEN_A]", "text": "Ελάτε ξανά"}, reason="no-show recovery")
    print("  ", d.status.value, "—", d.why)

    print("② after granting permission → sandbox + pending approval:")
    gov.grant("messaging:send")
    p = gov.propose("send_recovery_sms", {"to_token": "[TOKEN_A]", "text": "Ελάτε ξανά"}, reason="no-show recovery")
    print("  ", p.status.value, "| evidence:", p.evidence)

    print("③ human approves → executes (audited who/why/evidence):")
    ex = gov.approve(p.id, approver="dr_papadopoulos")
    print("   executed", ex.execution_id, "| outbox:", outbox)

    print("④ rollback:")
    gov.rollback(ex.execution_id, actor="dr_papadopoulos")
    print("   outbox after rollback:", outbox, "| audit intact:", AUDIT.verify())
