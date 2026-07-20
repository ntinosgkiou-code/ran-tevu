"""
test_hardening.py — Phase 0 substrate-hardening tests (stdlib only, no external deps).
Run:  python core/test_hardening.py     (or from core/:  python test_hardening.py)

Verifies the load-bearing fixes the code-map flagged before agents hang off the runtime:
  • exponential-backoff supervised restart capped at max_restarts → dead-letter (no restart storm)
  • restart-window reset (a long-healthy actor regains its restart budget)
  • graceful drain on shutdown (in-flight mailbox work finishes)
  • structured-PII (ΑΜΚΑ / IBAN) tokenized on ingress, restored on egress
  • appointment dates are NOT tokenized (booking flow stays intact)
  • GDPR-first compliance profile is the default floor
"""
import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from runtime import Actor, Supervisor, Message
import compliance
import config


def test_compliance_amka_iban():
    text = ("Είμαι ο Γιώργος Παπαδόπουλος, ΑΜΚΑ 12036512345, "
            "IBAN GR1601101250000000012300695, email g@x.gr")
    redacted, vault = compliance.anonymize_payload(text)
    assert "12036512345" not in redacted, "ΑΜΚΑ leaked into reasoning layer"
    assert "GR1601101250000000012300695" not in redacted, "IBAN leaked"
    assert "g@x.gr" not in redacted, "email leaked"
    assert vault.tokens_of_kind("AMKA"), "no AMKA token minted"
    assert vault.tokens_of_kind("IBAN"), "no IBAN token minted"
    restored, _ = compliance.deanonymize_response(redacted, vault)
    assert "12036512345" in restored and "GR1601101250000000012300695" in restored, "re-identification failed"
    print("✅ compliance: ΑΜΚΑ + IBAN + email tokenized on ingress, restored on egress")


def test_appointment_date_not_tokenized():
    text = "Θέλω ραντεβού στις 12/03/2026 στις 5"
    redacted, _ = compliance.anonymize_payload(text)
    assert "12/03/2026" in redacted, "appointment date wrongly tokenized → would break booking"
    print("✅ compliance: appointment date preserved (booking flow not broken)")


def test_compliance_profile_gdpr_default():
    p = config.compliance()
    assert p["name"] == "gdpr", f"expected gdpr default, got {p['name']}"
    assert p["breach_notify_hours"] == 72 and p["explicit_consent_required"]
    assert p["require_encryption_at_rest"] and p["require_audit_log"]
    print("✅ config: GDPR-first compliance profile is the default floor")


async def test_supervised_restart_and_dead_letter():
    sup = Supervisor(max_restarts=3, backoff=0.02, restart_window=60.0, max_backoff=0.2)

    async def always_crash(msg: Message):
        raise RuntimeError("boom")

    a = sup.register(Actor("doomed", always_crash))
    sup.start_all()
    for _ in range(6):
        await a.tell("go")          # 6 crashing messages queued across restarts
    await asyncio.sleep(1.0)         # let the supervised restarts (0.02→0.04→0.08 + jitter) play out
    assert a.restarts == 3, f"expected exactly max_restarts=3, got {a.restarts}"
    assert "doomed" in sup.dead_letters, f"actor should be dead-lettered; dead={sup.dead_letters}"
    await sup.shutdown()
    print("✅ runtime: supervised restarts capped at max_restarts → dead-letter (no infinite storm)")


async def test_restart_window_reset():
    sup = Supervisor(max_restarts=2, backoff=0.01, restart_window=0.15, max_backoff=0.05)
    state = {"crash": True, "n": 0}

    async def maybe_crash(msg: Message):
        state["n"] += 1
        if state["crash"]:
            raise RuntimeError("boom")
        return "ok"

    a = sup.register(Actor("flappy", maybe_crash))
    sup.start_all()
    await a.tell("go")               # crash once → restarts == 1
    await asyncio.sleep(0.05)
    assert a.restarts == 1, f"expected 1 restart, got {a.restarts}"
    state["crash"] = False           # actor is now healthy
    await asyncio.sleep(0.30)        # stay healthy longer than restart_window (0.15)
    state["crash"] = True
    await a.tell("go2")              # crash again after the healthy window
    await asyncio.sleep(0.1)
    assert a.restarts == 1, f"restart-window should have reset the counter; got {a.restarts}"
    await sup.shutdown()
    print("✅ runtime: restart-window reset — a long-healthy actor regains its restart budget")


async def test_graceful_drain():
    sup = Supervisor()
    done = {"n": 0}

    async def slow(msg: Message):
        await asyncio.sleep(0.05)
        done["n"] += 1

    a = sup.register(Actor("worker", slow))
    sup.start_all()
    for _ in range(5):
        await a.tell("job")
    await sup.shutdown(drain_timeout=2.0)   # must let all 5 finish before cancel
    assert done["n"] == 5, f"graceful drain lost work: {done['n']}/5 processed"
    print("✅ runtime: graceful shutdown drained in-flight mailbox (5/5 processed)")


def main():
    test_compliance_amka_iban()
    test_appointment_date_not_tokenized()
    test_compliance_profile_gdpr_default()
    asyncio.run(test_supervised_restart_and_dead_letter())
    asyncio.run(test_restart_window_reset())
    asyncio.run(test_graceful_drain())
    print("\n🎯 Phase 0 substrate hardening: ALL TESTS PASSED")


if __name__ == "__main__":
    main()
