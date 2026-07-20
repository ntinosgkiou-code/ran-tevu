"""
test_tools.py — governed tool-calling + credential broker (act inside real tools, safely).
Run: python core/test_tools.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from tools import build_demo_broker
from governance import Status
from audit import AUDIT


def test_tool_discovery():
    broker, _ = build_demo_broker()
    names = {t["name"] for t in broker.list_tools()}
    assert names == {"sms_send", "calendar_create", "crm_upsert"}
    assert all("input_schema" in t and "permission" in t for t in broker.list_tools())
    print("✅ discovery: tools expose MCP-style schema (name/permission/input_schema)")


def test_unpermitted_tool_call_denied():
    broker, backend = build_demo_broker()
    p = broker.call("sms_send", {"to": "[T]", "text": "hi"}, reason="recovery")
    assert p.status == Status.DENIED and backend["sms"] == [], "unpermitted tool call must not execute"
    print("✅ permission: tool call denied without the tool's permission")


def test_credentials_never_exposed_to_agent():
    broker, backend = build_demo_broker()
    broker.gov.grant("tool:sms")
    p = broker.call("sms_send", {"to": "[T]", "text": "Ελάτε ξανά"}, reason="recovery")
    # neither the sandbox evidence nor the (pending) proposal may contain the secret
    assert "SECRET_SMS_KEY" not in str(p.evidence) and "SECRET_SMS_KEY" not in str(p.model_dump())
    ex = broker.gov.approve(p.id, approver="dr_9")
    assert backend["sms"] == [{"to": "[T]", "text": "Ελάτε ξανά"}], "the tool executed (broker injected creds)"
    assert "SECRET_SMS_KEY" not in str(ex.model_dump()), "the credential must never reach the caller"
    print("✅ credential broker: tool executed with creds the agent never saw (Clawvisor pattern)")


def test_sandbox_preview_then_approval():
    broker, backend = build_demo_broker()
    broker.gov.grant("tool:sms")
    p = broker.call("sms_send", {"to": "[T]", "text": "Υπενθύμιση ραντεβού αύριο"}, reason="reminder")
    assert p.status == Status.PENDING_APPROVAL and p.evidence.get("preview")
    assert backend["sms"] == [], "must not send before approval"
    broker.gov.approve(p.id, approver="dr_9")
    assert len(backend["sms"]) == 1
    print("✅ sandbox+approval: preview shown, sent only after human approval")


def test_rollback_across_tools():
    broker, backend = build_demo_broker()
    for perm in ("tool:calendar", "tool:crm"):
        broker.gov.grant(perm)
    # calendar create → rollback deletes
    pc = broker.call("calendar_create", {"title": "Φυσικοθεραπεία", "slot": "2026-07-02T17:00"})
    ec = broker.gov.approve(pc.id, approver="dr_9")
    assert backend["calendar"], "event created"
    broker.gov.rollback(ec.execution_id, actor="dr_9")
    assert backend["calendar"] == {}, "rollback removed the calendar event"
    # crm upsert over an existing record → rollback restores prior
    backend["crm"]["c1"] = {"status": "lead"}
    pu = broker.call("crm_upsert", {"contact": "c1", "fields": {"status": "customer"}})
    eu = broker.gov.approve(pu.id, approver="dr_9")
    assert backend["crm"]["c1"] == {"status": "customer"}
    broker.gov.rollback(eu.execution_id, actor="dr_9")
    assert backend["crm"]["c1"] == {"status": "lead"}, "rollback restored the prior CRM record"
    print("✅ rollback: calendar event deleted · CRM contact restored to prior state")


def test_tool_actions_audited():
    broker, _ = build_demo_broker()
    broker.gov.grant("tool:sms")
    p = broker.call("sms_send", {"to": "[T]", "text": "x"}, reason="win-back")
    broker.gov.approve(p.id, approver="dr_smith")
    ev = [r for r in AUDIT.tail(12) if r["event"] == "gov.execute" and r["data"]["cap"] == "tool:sms_send"]
    assert ev and ev[-1]["data"]["approved_by"] == "dr_smith" and AUDIT.verify()
    print("✅ audit: every tool action logged (who/why/evidence), chain intact")


def main():
    test_tool_discovery()
    test_unpermitted_tool_call_denied()
    test_credentials_never_exposed_to_agent()
    test_sandbox_preview_then_approval()
    test_rollback_across_tools()
    test_tool_actions_audited()
    print("\n🎯 GOVERNED TOOL-CALLING (act inside real tools, safely): ALL TESTS PASSED")


if __name__ == "__main__":
    main()
