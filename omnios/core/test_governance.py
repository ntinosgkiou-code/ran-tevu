"""
test_governance.py — Action Governance / Trust Layer tests.
Verifies the guardrails the YC-2026 pattern says are REQUIRED once an agent ACTS:
permissions, sandbox dry-run, eval gate, human approval, audit (what/why/evidence/who), rollback.
Run: python core/test_governance.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from governance import ActionGovernor, Capability, Risk, Status
from audit import AUDIT


def _gov_with_sms():
    outbox: list = []
    gov = ActionGovernor(permissions=["ledger:read"])
    gov.register(Capability(
        name="send_sms", risk=Risk.HIGH, permission="messaging:send",
        sandbox=lambda p: {"to": p["to"], "preview": p["text"][:30]},
        execute=lambda p: outbox.append(p) or {"sent": p["to"]},
        compensate=lambda p, r: outbox.remove(p),
    ))
    return gov, outbox


def test_permission_denies_unpermitted_action():
    gov, outbox = _gov_with_sms()
    d = gov.propose("send_sms", {"to": "[TOK]", "text": "hi"}, reason="recovery")
    assert d.status == Status.DENIED and "messaging:send" in d.why
    assert outbox == [], "a denied action must NOT execute"
    print("✅ permissions: unpermitted action denied, nothing executed")


def test_sandbox_then_human_approval_then_execute():
    gov, outbox = _gov_with_sms()
    gov.grant("messaging:send")
    p = gov.propose("send_sms", {"to": "[TOK]", "text": "Ελάτε ξανά"}, reason="no-show recovery")
    assert p.status == Status.PENDING_APPROVAL, "high-risk must wait for a human"
    assert p.evidence.get("preview"), "sandbox must produce a preview (evidence) before executing"
    assert outbox == [], "must not execute before approval"
    ex = gov.approve(p.id, approver="dr_9")
    assert ex.status == Status.EXECUTED and ex.execution_id and outbox and outbox[0]["to"] == "[TOK]"
    print("✅ sandbox+approval: preview → human approves → executes (not before)")


def test_low_risk_auto_executes():
    gov = ActionGovernor(permissions=["ledger:read"])
    store = {"reads": 0}
    gov.register(Capability(name="read_ledger", risk=Risk.LOW, permission="ledger:read",
                            execute=lambda p: store.__setitem__("reads", store["reads"] + 1) or {"ok": True}))
    p = gov.propose("read_ledger", {})
    assert p.status == Status.EXECUTED and store["reads"] == 1, "low-risk permitted action auto-executes"
    print("✅ risk routing: low-risk permitted action auto-executes (still audited)")


def test_eval_gate_blocks():
    gov = ActionGovernor(permissions=["msg"])
    gov.register(Capability(name="risky", risk=Risk.HIGH, permission="msg",
                            sandbox=lambda p: {"amount": p["amount"]},
                            eval_gate=lambda p: p["amount"] <= 100,   # refuse large amounts
                            execute=lambda p: {"charged": p["amount"]}))
    blocked = gov.propose("risky", {"amount": 500})
    assert blocked.status == Status.BLOCKED, "eval gate must block"
    ok = gov.propose("risky", {"amount": 50})
    assert ok.status == Status.PENDING_APPROVAL, "passing the eval gate proceeds to approval"
    print("✅ evals: eval gate blocks a failing action, passes a valid one")


def test_rollback_compensates():
    gov, outbox = _gov_with_sms()
    gov.grant("messaging:send")
    p = gov.propose("send_sms", {"to": "[TOK]", "text": "x"}, reason="r")
    ex = gov.approve(p.id, approver="dr_9")
    assert len(outbox) == 1
    assert gov.rollback(ex.execution_id, actor="dr_9") is True
    assert outbox == [], "rollback must undo the executed action"
    assert gov.rollback(ex.execution_id, actor="dr_9") is False, "double rollback is a no-op"
    print("✅ rollback: executed action compensated (undone) and idempotent")


def test_audit_captures_who_why_evidence():
    gov, _ = _gov_with_sms()
    gov.grant("messaging:send")
    p = gov.propose("send_sms", {"to": "[TOK]", "text": "y"}, reason="win-back")
    gov.approve(p.id, approver="dr_smith")
    ev = [r for r in AUDIT.tail(10) if r["event"] == "gov.execute"]
    assert ev, "execution must be audited"
    d = ev[-1]["data"]
    assert d["approved_by"] == "dr_smith" and d["reason"] == "win-back" and "evidence" in d
    assert AUDIT.verify() is True
    print("✅ audit: every execution records WHO approved, WHY, and the EVIDENCE (chain intact)")


def main():
    test_permission_denies_unpermitted_action()
    test_sandbox_then_human_approval_then_execute()
    test_low_risk_auto_executes()
    test_eval_gate_blocks()
    test_rollback_compensates()
    test_audit_captures_who_why_evidence()
    print("\n🎯 ACTION GOVERNANCE (permissions·sandbox·evals·approval·audit·rollback): ALL TESTS PASSED")


if __name__ == "__main__":
    main()
