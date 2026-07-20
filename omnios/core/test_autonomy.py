"""
test_autonomy.py — the policy-based autonomous mode: does everything within the doctor's rules,
proves it, and never crosses consent or the kill switch. Run: python core/test_autonomy.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clinic_store import ClinicStore
from clinic_autopilot import ClinicAutopilot
from autonomy_policy import AutonomyPolicy, default_policy


def _clinic_with_work():
    cs = ClinicStore(":memory:"); cs.setup_clinic(name="T", specialty="Φυσικοθεραπεία", default_value=40)
    p = cs.add_patient("Α. Ασθενής", phone="6900000001", consent=True)
    cs.add_appointment("10:00", "Α. Ασθενής", date="2026-08-01", status="no_show",
                       value=40, patient_id=p["id"])
    return cs


def test_off_by_default_nothing_auto_executes():
    cs = _clinic_with_work()
    ap = ClinicAutopilot(cs)
    r = ap.run_autonomous()
    assert r["autonomy_enabled"] is False
    assert r["executed_auto"] == 0 and r["awaiting_human"] >= 1, "must propose only when autonomy is off"
    print("✅ opt-in: autonomy OFF by default → 0 auto-executed, work awaits the human")


def test_policy_authorizes_execution():
    cs = _clinic_with_work()
    AutonomyPolicy(cs).set(enabled=True, rules={"no_show_recovery": {"auto": True, "max_eur": 500}},
                           set_by="dr_test")
    ap = ClinicAutopilot(cs)
    r = ap.run_autonomous()
    assert r["autonomy_enabled"] is True
    assert r["executed_auto"] >= 1, "an enabled auto-rule must execute the matching proposal"
    # it really dispatched (outbox), exactly once, tagged 'policy'
    done = [p for p in ap.all() if p["status"] == "approved" and p["type"] == "no_show_recovery"]
    assert done and done[0].get("via") == "policy"
    print(f"✅ policy executes: {r['executed_auto']} action(s) ran autonomously, tagged via=policy")


def test_euro_ceiling_falls_back_to_human():
    cs = ClinicStore(":memory:"); cs.setup_clinic(name="T", specialty="x", default_value=40)
    p = cs.add_patient("Β", phone="6900000002", consent=True)
    cs.add_appointment("11:00", "Β", date="2026-08-01", status="no_show", value=900, patient_id=p["id"])
    AutonomyPolicy(cs).set(enabled=True, rules={"no_show_recovery": {"auto": True, "max_eur": 500}})
    ap = ClinicAutopilot(cs)
    r = ap.run_autonomous()
    assert r["executed_auto"] == 0 and r["awaiting_human"] >= 1, "€900 > €500 ceiling must await a human"
    print("✅ ceiling: a €900 action over the €500 cap falls back to human review")


def test_consent_still_blocks_even_with_autonomy_on():
    cs = ClinicStore(":memory:"); cs.setup_clinic(name="T", specialty="x", default_value=40)
    p = cs.add_patient("Γ", phone="6900000003", consent=False)   # withheld consent
    cs.add_appointment("12:00", "Γ", date="2026-08-01", status="no_show", value=40, patient_id=p["id"])
    AutonomyPolicy(cs).set(enabled=True, rules={"no_show_recovery": {"auto": True, "max_eur": 500}})
    ap = ClinicAutopilot(cs)
    r = ap.run_autonomous()
    assert r["executed_auto"] == 0, "consent=False must never be contacted, autonomy or not (Art.9)"
    print("✅ consent supremacy: autonomy ON still never contacts a non-consenting patient")


def test_kill_switch_stops_everything_instantly():
    cs = _clinic_with_work()
    pol = AutonomyPolicy(cs)
    pol.set(enabled=True, rules={"no_show_recovery": {"auto": True, "max_eur": 500}})
    pol.set(enabled=False)                       # flip the master switch off
    ap = ClinicAutopilot(cs)
    r = ap.run_autonomous()
    assert r["executed_auto"] == 0, "kill switch (enabled=False) must stop all auto-execution"
    print("✅ kill switch: master OFF halts all autonomous execution immediately")


def test_every_auto_execution_is_audited_with_policy():
    from audit import AUDIT
    cs = _clinic_with_work()
    AutonomyPolicy(cs).set(enabled=True, rules={"no_show_recovery": {"auto": True, "max_eur": 500}},
                           set_by="dr_audit")
    ap = ClinicAutopilot(cs)
    ap.run_autonomous()
    events = [e for e in AUDIT.chain(500) if e.get("event") == "autopilot.auto_execute"]
    assert events, "every autonomous execution must be on the tamper-evident chain"
    assert any(e["data"].get("policy_by") == "dr_audit" for e in events), "audit must name the policy setter"
    assert AUDIT.verify(), "the chain must stay intact"
    print("✅ flight recorder: each auto-execution is audited WITH the authorizing policy")


def test_concurrent_runs_execute_each_action_once():
    """THE race the adversarial review caught: the heartbeat + the HTTP endpoint share one autopilot
    instance and can call run_autonomous() concurrently. Every proposal must still fire its real
    side-effect EXACTLY ONCE (no duplicate SMS / invoice), not once-per-thread."""
    from concurrent.futures import ThreadPoolExecutor
    cs = ClinicStore(":memory:"); cs.setup_clinic(name="T", specialty="Φυσικοθεραπεία", default_value=40)
    for i in range(8):
        p = cs.add_patient(f"Ασθενής {i}", phone=f"697100000{i}", consent=True)
        cs.add_appointment(f"1{i%9}:00", f"Ασθενής {i}", date="2026-08-01", status="no_show",
                           value=40, patient_id=p["id"])
    AutonomyPolicy(cs).set(enabled=True, rules={"no_show_recovery": {"auto": True, "max_eur": 500}})
    ap = ClinicAutopilot(cs)                      # ONE shared instance, like api.py

    def spy_dispatch(orig):
        calls = {"n": 0}
        def wrapped(*a, **k):
            calls["n"] += 1
            return orig(*a, **k)
        return calls, wrapped
    calls, wrapped = spy_dispatch(ap.channels.dispatch)
    ap.channels.dispatch = wrapped

    with ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(lambda _: ap.run_autonomous(), range(20)))
    approved = [p for p in ap.all() if p["type"] == "no_show_recovery" and p["status"] == "approved"]
    # 8 proposals, each must dispatch exactly once no matter how many threads raced
    assert calls["n"] == 8, f"duplicate side-effects under concurrency: {calls['n']} dispatches for 8 proposals"
    assert len(approved) == 8
    print(f"✅ concurrency: 20 racing run_autonomous() → {calls['n']} dispatches for 8 proposals (exactly once)")


def test_zero_ceiling_does_not_mean_uncapped():
    """A priced action with max_eur=0 must NOT auto-execute (fail-safe), not slip through uncapped."""
    cs = ClinicStore(":memory:"); cs.setup_clinic(name="T", specialty="x", default_value=40)
    p = cs.add_patient("Δ", phone="6900000004", consent=True)
    cs.add_appointment("13:00", "Δ", date="2026-08-01", status="no_show", value=40, patient_id=p["id"])
    AutonomyPolicy(cs).set(enabled=True, rules={"no_show_recovery": {"auto": True, "max_eur": 0}})
    ap = ClinicAutopilot(cs)
    r = ap.run_autonomous()
    assert r["executed_auto"] == 0, "max_eur=0 on a priced action must fall back to human, not auto-send"
    print("✅ zero-ceiling safety: a priced action with €0 ceiling stays with the human")


def test_consent_revoked_after_proposal_blocks_execution():
    """Consent is re-checked at EXECUTION: revoke after the proposal exists → it must not send."""
    cs = ClinicStore(":memory:"); cs.setup_clinic(name="T", specialty="x", default_value=40)
    p = cs.add_patient("Ε", phone="6900000005", consent=True)
    cs.add_appointment("14:00", "Ε", date="2026-08-01", status="no_show", value=40, patient_id=p["id"])
    ap = ClinicAutopilot(cs)
    ap.sweep()                                   # proposal filed while consent=True
    cs.update_patient(p["id"], consent=False)    # patient revokes BEFORE the auto-run
    AutonomyPolicy(cs).set(enabled=True, rules={"no_show_recovery": {"auto": True, "max_eur": 500}})
    prop = next(x for x in ap.pending() if x["type"] == "no_show_recovery")
    res = ap.approve(prop["id"], "autopilot·policy", via="policy")
    assert res["status"] == "blocked_no_consent", "revoked consent must block execution at run time"
    print("✅ consent re-check: revoking consent after the proposal blocks the send at execution")


def main():
    test_off_by_default_nothing_auto_executes()
    test_policy_authorizes_execution()
    test_euro_ceiling_falls_back_to_human()
    test_consent_still_blocks_even_with_autonomy_on()
    test_kill_switch_stops_everything_instantly()
    test_every_auto_execution_is_audited_with_policy()
    test_concurrent_runs_execute_each_action_once()
    test_zero_ceiling_does_not_mean_uncapped()
    test_consent_revoked_after_proposal_blocks_execution()
    print("\n🎯 AUTONOMY POLICY MODE: ALL TESTS PASSED — self-driving, capped, consent-safe, provable")


if __name__ == "__main__":
    main()
