"""
test_browser.py — bounded browser-automation capability (no-API portals, governed).
Run: python core/test_browser.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from governance import ActionGovernor, Status
from browser import BrowserAgent
from audit import AUDIT

URL = "https://eservices.eopyy.gov.gr/eDAPY/referral"


def _agent(*perms):
    gov = ActionGovernor(permissions=list(perms))
    return gov, BrowserAgent(gov)


def test_read_is_low_risk_auto():
    gov, ba = _agent("browser:read")
    r = ba.read(URL, "#referral_code", reason="collect referral")
    assert r.status == Status.EXECUTED and r.result == "REF-2026-123", "reading a page is auto (low-risk)"
    print("✅ read: page data extracted automatically (low-risk, audited)")


def test_unpermitted_actions_denied():
    gov, ba = _agent()   # no permissions
    assert ba.read(URL, "#referral_code").status == Status.DENIED
    assert ba.fill("#otp", "1").status == Status.DENIED
    assert ba.submit("f").status == Status.DENIED
    print("✅ permissions: read/fill/submit all denied without their grants")


def test_fill_requires_approval_and_is_reversible():
    gov, ba = _agent("browser:fill")
    p = ba.fill("#otp", "123456", reason="prefill execution")
    assert p.status == Status.PENDING_APPROVAL, "filling a portal field needs approval"
    assert ba.driver.fields == {}, "must not fill before approval"
    ex = gov.approve(p.id, approver="secretary")
    assert ba.driver.fields == {"#otp": "123456"}
    assert gov.rollback(ex.execution_id, actor="secretary") is True and ba.driver.fields == {}, "fill is reversible"
    print("✅ fill: approval-gated and reversible (clearable before submit)")


def test_submit_needs_clinician_and_is_bounded():
    gov, ba = _agent("browser:fill", "browser:submit")
    gov.approve(ba.fill("#otp", "123456").id, approver="secretary")
    s = ba.submit("eDAPY_execution", reason="ΕΟΠΥΥ μηνιαία υποβολή")
    assert s.status == Status.PENDING_APPROVAL, "the legally-binding submit must wait for a human"
    assert ba.driver.submitted == [], "nothing submitted before the doctor approves"
    done = gov.approve(s.id, approver="dr_papadopoulos")
    assert done.status == Status.EXECUTED and ba.driver.submitted, "doctor's approval performs the submission"
    # audit records WHO submitted (the clinician), on their action
    ev = [r for r in AUDIT.tail(15) if r["event"] == "gov.execute" and r["data"]["cap"] == "browser.submit"]
    assert ev and ev[-1]["data"]["approved_by"] == "dr_papadopoulos" and AUDIT.verify()
    print("✅ submit: bounded — never auto; the DOCTOR approves (human at the trust boundary), audited")


def main():
    test_read_is_low_risk_auto()
    test_unpermitted_actions_denied()
    test_fill_requires_approval_and_is_reversible()
    test_submit_needs_clinician_and_is_bounded()
    print("\n🎯 BOUNDED BROWSER AUTOMATION (no-API portals, governed): ALL TESTS PASSED")


if __name__ == "__main__":
    main()
