"""
browser.py — Bounded browser-automation Capability (act inside no-API portals, safely).
=======================================================================================
Some systems the clinic depends on have NO API — e.g. the Greek ΕΟΠΥΥ eΔΑΠΥ portal. The YC-2026
pattern's answer is a governed browser agent. Here it is, but BOUNDED for a regulated flow:

  • read  — extract data from a page (low-risk, auto, audited).
  • fill  — prepare a form field (high-risk → human approval; reversible/clearable).
  • submit — the legally-binding action (high-risk → a CLINICIAN must approve; it is the doctor's
             action on their own credentials — the "human at the trust boundary"). Never auto.

Everything flows through the ActionGovernor (permission→sandbox→approval→execute→audit→rollback).
A real Playwright/Selenium driver drops in behind the `BrowserDriver` methods — the governance
flow is unchanged. Deterministic mock driver here so it runs offline. Tests: python core/test_browser.py
"""
from __future__ import annotations

from typing import Any, Optional

from governance import ActionGovernor, Capability, Proposal, Risk


class MockBrowser:
    """Deterministic stand-in for a real browser. Real Playwright implements the same 5 methods."""

    _PAGES = {
        "https://eservices.eopyy.gov.gr/eDAPY/referral": {
            "#patient": "[TOKEN_PATIENT]", "#referral_code": "REF-2026-123", "#sessions_left": "6",
        },
    }

    def __init__(self) -> None:
        self.url: Optional[str] = None
        self.fields: dict[str, Any] = {}
        self.submitted: list[dict] = []

    def navigate(self, url: str) -> dict:
        self.url = url
        return {"url": url, "found": url in self._PAGES}

    def read(self, selector: str) -> Optional[str]:
        return self._PAGES.get(self.url or "", {}).get(selector)

    def fill(self, selector: str, value: Any) -> dict:
        self.fields[selector] = value
        return {"filled": selector}

    def clear_field(self, selector: str) -> None:
        self.fields.pop(selector, None)

    def submit(self, form: str) -> dict:
        rec = {"form": form, "fields": dict(self.fields), "url": self.url}
        self.submitted.append(rec)
        return {"confirmation": f"EOPYY-{len(self.submitted):04d}", "form": form}


class BrowserAgent:
    """Registers bounded browser actions as governed capabilities."""

    def __init__(self, governor: ActionGovernor, driver: Optional[MockBrowser] = None) -> None:
        self.gov = governor
        self.driver = driver or MockBrowser()
        self._register()

    def _register(self) -> None:
        def _read(a: dict):
            if a.get("url"):
                self.driver.navigate(a["url"])
            return self.driver.read(a["selector"])

        self.gov.register(Capability(
            name="browser.read", risk=Risk.LOW, permission="browser:read",
            sandbox=lambda a: {"action": "read", "url": a.get("url"), "selector": a["selector"]},
            execute=_read))

        self.gov.register(Capability(
            name="browser.fill", risk=Risk.HIGH, permission="browser:fill",
            sandbox=lambda a: {"action": "fill", "selector": a["selector"], "preview": str(a["value"])[:40]},
            execute=lambda a: self.driver.fill(a["selector"], a["value"]),
            compensate=lambda a, r: self.driver.clear_field(a["selector"])))

        self.gov.register(Capability(
            name="browser.submit", risk=Risk.HIGH, permission="browser:submit",
            sandbox=lambda a: {"action": "submit", "form": a["form"], "fields": dict(self.driver.fields),
                               "note": "νομικά δεσμευτικό — υποβάλλει ο γιατρός (human at the boundary)"},
            execute=lambda a: self.driver.submit(a["form"])))   # no compensate: a real submission is irreversible → the approval gate IS the control

    # agent-facing (returns governed Proposals; nothing acts without going through the governor)
    def read(self, url: str, selector: str, reason: str = "") -> Proposal:
        return self.gov.propose("browser.read", {"url": url, "selector": selector}, reason=reason)

    def fill(self, selector: str, value: Any, reason: str = "") -> Proposal:
        return self.gov.propose("browser.fill", {"selector": selector, "value": value}, reason=reason)

    def submit(self, form: str, reason: str = "") -> Proposal:
        return self.gov.propose("browser.submit", {"form": form}, reason=reason)


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    gov = ActionGovernor(permissions=["browser:read", "browser:fill", "browser:submit"])
    ba = BrowserAgent(gov)
    URL = "https://eservices.eopyy.gov.gr/eDAPY/referral"

    print("① READ referral (auto, low-risk):")
    r = ba.read(URL, "#referral_code", reason="collect ΕΟΠΥΥ referral")
    print("   ", r.status.value, "→", r.result)

    print("② PREFILL the execution OTP field (needs approval):")
    p = ba.fill("#otp", "123456", reason="prefill eΔΑΠΥ execution")
    print("   ", p.status.value, "| preview:", p.evidence.get("preview"))
    ex = gov.approve(p.id, approver="secretary")
    print("    filled → fields:", ba.driver.fields)

    print("③ SUBMIT (legally binding → the DOCTOR approves; never auto):")
    s = ba.submit("eDAPY_execution", reason="ΕΟΠΥΥ μηνιαία υποβολή")
    print("   ", s.status.value, "—", s.why)
    done = gov.approve(s.id, approver="dr_papadopoulos(ΑΦΜ)")
    print("    submitted by doctor →", done.result)

    print("④ rollback the prefill (before submit) is possible:")
    p2 = ba.fill("#note", "λάθος", reason="typo")
    ex2 = gov.approve(p2.id, approver="secretary")
    gov.rollback(ex2.execution_id, actor="secretary")
    print("    fields after rollback:", ba.driver.fields)
