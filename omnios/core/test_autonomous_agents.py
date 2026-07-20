"""
test_autonomous_agents.py — οι agents δρουν ΠΡΟΛΗΠΤΙΚΑ και συνεννοούνται στο ΙΔΙΟ bus.
Αποδεικνύει: ένα tick() σαρώνει πραγματικά δεδομένα → στέλνει ΜΟΝΟ την εξουσιοδοτημένη (policy)
δουλειά στο bus → οι specialist agents την εκτελούν μέσω του ασφαλούς approve() (idempotent, consent,
audited) → αναφέρουν πίσω. Μη-εξουσιοδοτημένη δουλειά μένει για τον γιατρό. Reactive + proactive
agents μοιράζονται ΕΝΑ bus. Τρέχει: python core/test_autonomous_agents.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clinic_store import ClinicStore
from clinic_autopilot import ClinicAutopilot
from autonomy_policy import AutonomyPolicy
from autonomous_agents import AutonomyOrchestrator
from clinic_bridge import ClinicEventBus, ClinicBridge
from datetime import datetime, timedelta

fails = []
def chk(label, ok):
    print(f"  {'✅' if ok else '❌ ΣΚΑΣΕ'} {label}")
    if not ok:
        fails.append(label)

YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def _clinic(tenant):
    cs = ClinicStore(":memory:", tenant=tenant)
    cs.setup_clinic(name="Ιατρείο", specialty="Φυσικοθεραπεία", default_value=40.0)
    return cs


def test_policy_gates_autonomous_dispatch():
    cs = _clinic("auto-1")
    pa = cs.add_patient("Συναινεί", phone="6970000001", consent=True)
    cs.add_appointment("10:00", "Συναινεί", "Συνεδρία", YESTERDAY, 40.0, "no_show", pa["id"])   # → recovery
    cs.add_invoice("Συναινεί", 40.0, "unpaid", pa["id"])                                        # → dunning
    ap = ClinicAutopilot(cs)
    # Πολιτική: ON, auto ΜΟΝΟ no_show_recovery (πλαφόν €200)· dunning ΟΧΙ auto.
    AutonomyPolicy(cs).set(enabled=True,
                           rules={"no_show_recovery": {"auto": True, "max_eur": 200}},
                           set_by="dr")
    orch = AutonomyOrchestrator(cs, ap)
    res = orch.tick()
    chk("tick: εξουσιοδοτημένη δουλειά στάλθηκε (dispatched≥1)", res["dispatched"] >= 1)
    chk("tick: μη-εξουσιοδοτημένη (dunning) ΔΕΝ στάλθηκε — έμεινε για γιατρό (skipped_policy≥1)",
        res["skipped_policy"] >= 1)
    j = orch.bus.journey()
    chk("Bus: work.recovery event στάλθηκε από την autonomy", any(e["type"] == "work.recovery" for e in j))
    chk("Bus: agent ανέφερε work.recovery.done (εκτέλεσε & απάντησε)",
        any(e["type"] == "work.recovery.done" for e in j))
    done = next(e for e in j if e["type"] == "work.recovery.done")
    chk("Ο recovery agent εκτέλεσε πραγματικά (status=approved)", done["payload"]["status"] == "approved")
    outbox = ap.channels.outbox()
    chk("Πραγματικό side-effect: μήνυμα ανάκτησης στην ουρά (keyless→queued, όχι ψεύτικο)",
        len(outbox) >= 1)


def test_idempotent_second_tick():
    cs = _clinic("auto-2")
    pa = cs.add_patient("Α", phone="6970000002", consent=True)
    cs.add_appointment("11:00", "Α", "Συνεδρία", YESTERDAY, 40.0, "no_show", pa["id"])
    ap = ClinicAutopilot(cs)
    AutonomyPolicy(cs).set(enabled=True, rules={"no_show_recovery": {"auto": True, "max_eur": 200}}, set_by="dr")
    orch = AutonomyOrchestrator(cs, ap)
    first = orch.tick()["dispatched"]
    second = orch.tick()["dispatched"]
    chk("2ο tick δεν ξανα-εκτελεί (dedupe + idempotent approve)", first >= 1 and second == 0)


def test_autonomy_off_dispatches_nothing():
    cs = _clinic("auto-3")
    pa = cs.add_patient("Β", phone="6970000003", consent=True)
    cs.add_appointment("12:00", "Β", "Συνεδρία", YESTERDAY, 40.0, "no_show", pa["id"])
    ap = ClinicAutopilot(cs)
    # Πολιτική OFF (master switch) — τίποτα δεν εκτελείται μόνο του
    orch = AutonomyOrchestrator(cs, ap)
    res = orch.tick()
    chk("Autonomy OFF → 0 dispatched, όλα μένουν για τον γιατρό",
        res["dispatched"] == 0 and res["awaiting_human"] >= 1)


def test_consent_blocks_at_source():
    cs = _clinic("auto-4")
    pa = cs.add_patient("ΔΕΝ Συναινεί", phone="6970000004", consent=False)
    cs.add_appointment("13:00", "ΔΕΝ Συναινεί", "Συνεδρία", YESTERDAY, 40.0, "no_show", pa["id"])
    ap = ClinicAutopilot(cs)
    AutonomyPolicy(cs).set(enabled=True, rules={"no_show_recovery": {"auto": True, "max_eur": 200}}, set_by="dr")
    orch = AutonomyOrchestrator(cs, ap)
    res = orch.tick()
    chk("Χωρίς συναίνεση → καμία πρόταση επικοινωνίας (consent gate στο propose-time) → 0 dispatched",
        res["dispatched"] == 0)


def test_reactive_and_proactive_share_one_bus():
    """Οι journey agents (reactive) + οι autonomous agents (proactive) στο ΙΔΙΟ nervous system."""
    cs = _clinic("auto-5")
    pa = cs.add_patient("Γ", phone="6970000005", consent=True)
    cs.add_appointment("14:00", "Γ", "Συνεδρία", YESTERDAY, 40.0, "no_show", pa["id"])
    ap = ClinicAutopilot(cs)
    AutonomyPolicy(cs).set(enabled=True, rules={"no_show_recovery": {"auto": True, "max_eur": 200}}, set_by="dr")
    bus = ClinicEventBus()
    bridge = ClinicBridge(cs, bus=bus)                       # reactive agents στο ίδιο bus
    orch = AutonomyOrchestrator(cs, ap, bus=bus)            # proactive agents στο ίδιο bus
    bridge.request_booking("Νέος", "15:00", "2026-08-01", value=40.0)   # reactive: κράτηση
    orch.tick()                                             # proactive: αυτόνομη σάρωση
    types = {e["type"] for e in bus.journey()}
    chk("ΕΝΑ bus κουβαλά ΚΑΙ reactive (appointment.booked) ΚΑΙ proactive (work.recovery)",
        "appointment.booked" in types and "work.recovery" in types)
    feed = orch.feed()
    chk("feed() δίνει το τι έκανε ο κάθε agent (για UI live view)", len(feed) >= 1)


if __name__ == "__main__":
    print("🤖 AUTONOMOUS AGENTS — προληπτική δράση + επικοινωνία στο bus:")
    test_policy_gates_autonomous_dispatch()
    test_idempotent_second_tick()
    test_autonomy_off_dispatches_nothing()
    test_consent_blocks_at_source()
    test_reactive_and_proactive_share_one_bus()
    print()
    if fails:
        print(f"❌ {len(fails)} ΑΠΟΤΥΧΙΕΣ: {fails}")
        sys.exit(1)
    print("✅ AUTONOMOUS AGENTS: proactive σάρωση, policy-gated, consent-safe, idempotent,")
    print("   reactive+proactive σε ΕΝΑ bus — αυτο-οδηγούμενο back office, πλήρως ιχνηλατήσιμο.")
