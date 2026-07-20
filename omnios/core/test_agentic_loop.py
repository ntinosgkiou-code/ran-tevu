"""
test_agentic_loop.py — the closed agentic loop (ingest → decide → act → update/improve → audit).
Run: python core/test_agentic_loop.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from agentic_loop import AgenticClinicAgent, OutcomeLearner
from audit import AUDIT


def test_decide_produces_audited_action():
    agent = AgenticClinicAgent()
    before = len(AUDIT)
    d = agent.ingest({"type": "appointment", "id": "A1", "value": 45})
    assert d["recommended"] != "none" and d["expected_value_gain"] > 0
    assert len(AUDIT) == before + 1, "the decision must be audited (act step)"
    print(f"✅ decide+act: recommended '{d['recommended']}' (+€{d['expected_value_gain']}), audited")


def test_learner_improves_toward_true_rate():
    agent = AgenticClinicAgent()   # prior 0.23 (published outpatient baseline)
    start = agent.learner.no_show_prob()
    # this clinic's TRUE no-show rate is 0.5
    outcomes = ([True, False] * 20)   # 20 shows, 20 no-shows → 0.5
    for i, showed in enumerate(outcomes):
        agent.ingest({"type": "appointment", "id": f"B{i}", "value": 45})
        agent.ingest({"type": "outcome", "id": f"B{i}", "showed": showed})
    learned = agent.learner.no_show_prob()
    assert learned > start, "estimate must move up from the 0.23 prior toward the true 0.5"
    assert abs(learned - 0.5) < abs(start - 0.5), "learned estimate must be CLOSER to the truth than the prior"
    print(f"✅ update/improve: no-show estimate {round(start,3)} → {round(learned,3)} (true 0.5) over {agent.learner.n} outcomes")


def test_recovered_revenue_attributed_on_showed():
    agent = AgenticClinicAgent()
    agent.ingest({"type": "appointment", "id": "R1", "value": 100})
    r = agent.ingest({"type": "outcome", "id": "R1", "showed": True})   # at-risk appt showed after intervention
    assert r["recovered_eur"] > 0, "a saved at-risk appointment must credit recovered €"
    # a no-show credits nothing
    agent.ingest({"type": "appointment", "id": "R2", "value": 100})
    r2 = agent.ingest({"type": "outcome", "id": "R2", "showed": False})
    assert r2["recovered_eur"] == r["recovered_eur"], "a no-show must not add recovered €"
    print(f"✅ ledger: recovered €{r2['recovered_eur']} attributed only to saved appointments")


def test_report_is_understandable_and_verifiable():
    agent = AgenticClinicAgent()
    agent.ingest({"type": "appointment", "id": "X", "value": 45})
    agent.ingest({"type": "outcome", "id": "X", "showed": True})
    rep = agent.report()
    assert rep.audit_chain_intact is True and rep.audit_records >= 2
    assert rep.audit_tail and "event" in rep.audit_tail[-1]
    print(f"✅ audit/understandable: {rep.audit_records} records, chain intact, human-readable tail")


def test_audit_tamper_is_detected():
    # append then tamper a past record → the hash chain must break (EU AI Act traceability)
    AUDIT.record("agent.decide", {"appt": "T", "recommend": "sms_reminder"})
    assert AUDIT.verify() is True
    AUDIT._records[-1].data["recommend"] = "require_deposit"   # silently alter a past decision
    assert AUDIT.verify() is False, "tampering with a past record must be detected"
    print("✅ tamper-evidence: altering a past audited decision breaks the chain (detected)")


def test_unknown_event_rejected():
    try:
        AgenticClinicAgent().ingest({"type": "nonsense"})
        assert False, "unknown event must raise"
    except ValueError:
        pass
    print("✅ guard: unknown event type rejected")


def main():
    test_decide_produces_audited_action()
    test_learner_improves_toward_true_rate()
    test_recovered_revenue_attributed_on_showed()
    test_report_is_understandable_and_verifiable()
    test_unknown_event_rejected()
    test_audit_tamper_is_detected()   # must be LAST — it corrupts the shared audit chain on purpose
    print("\n🎯 AGENTIC LOOP (ingest→decide→act→update/improve→audit): ALL TESTS PASSED")


if __name__ == "__main__":
    main()
