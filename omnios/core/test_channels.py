"""
test_channels.py — the outbound channel layer + check-in flow + persisted clinical notes.
Proves: key-less mode queues durably with an honest reason (never pretends to send), empty
contacts are blocked, approved autopilot outreach flows through the dispatcher, the arrived→
completed check-in path counts visits at the RIGHT moment, and signed notes survive restarts.
Run: python core/test_channels.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from channels import MessageChannels
from clinic_autopilot import ClinicAutopilot
from clinic_store import ClinicStore


def test_keyless_dispatch_queues_honestly():
    cs = ClinicStore(":memory:")
    ch = MessageChannels(cs)
    assert not ch.sms_ready and not ch.email_ready       # the suite runs key-less by design
    r = ch.dispatch("sms", "+306945123456", "Υπενθύμιση ραντεβού αύριο 17:00", why="reminder")
    assert r["status"] == "queued" and "provider keys" in r["reason"]
    assert ch.status()["mode"].startswith("OUTBOX") and ch.status()["queued"] == 1
    assert ch.status()["sent"] == 0, "must NEVER report sent without a provider"
    print("✅ key-less: dispatch → durable outbox with the honest reason (0 pretend-sends)")


def test_empty_contact_blocked():
    cs = ClinicStore(":memory:")
    ch = MessageChannels(cs)
    r = ch.dispatch("sms", "", "μήνυμα", why="recovery")
    assert r["status"] == "blocked" and "no contact" in r["reason"]
    print("✅ no phone on record → BLOCKED with reason (never claims outreach it can't deliver)")


def test_autopilot_approval_flows_through_channels():
    cs = ClinicStore(":memory:")
    cs.seed_demo()
    ap = ClinicAutopilot(cs)
    ap.sweep()
    ns = next(p for p in ap.pending() if p["type"] == "no_show_recovery")
    done = ap.approve(ns["id"], "dr_giatros")
    assert done["executed"].startswith("sms queued"), done["executed"]
    ob = ap.channels.outbox()
    assert len(ob) == 1 and ob[0]["approved_by"] == "dr_giatros"
    assert ob[0]["to"], "the real patient phone was resolved from the store"
    print(f"✅ approve → channel dispatch: '{done['executed']}' · outbox carries who approved + real phone")


def test_checkin_arrived_then_completed():
    cs = ClinicStore(":memory:")
    cs.setup_clinic(name="Τεστ")
    p = cs.add_patient("Α. Ασθενής", phone="6970000001")
    a = cs.add_appointment("09:00", "Α. Ασθενής", patient_id=p["id"])
    cs.set_status(a["id"], "arrived")                    # check-in: in the waiting room
    assert cs.get_patient(p["id"])["visits"] == 0, "arrival must NOT count a visit"
    assert not cs.list_invoices(), "arrival must NOT invoice"
    # the slot stays occupied while arrived
    try:
        cs.add_appointment("09:00", "Άλλος")
        assert False, "arrived must still occupy the slot"
    except ValueError:
        pass
    cs.set_status(a["id"], "completed")                  # visit done
    pat = cs.get_patient(p["id"])
    assert pat["visits"] == 1 and "/" in pat["last_visit"], "real date, not a literal"
    assert len(cs.list_invoices()) == 1                  # auto-invoice fired at the right moment
    print("✅ check-in: arrived (0 visits, slot held) → completed (visit+1, real date, auto-invoice)")


def test_signed_notes_persist_across_restart():
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        cs = ClinicStore(path, tenant="t1")
        cs.add_note({"id": "note-1", "status": "SIGNED", "signed_by": "dr_giatros",
                     "problems": ["I10"], "transcript_redacted": "[REDACTED] υπέρταση"})
        cs.kv.close()
        cs2 = ClinicStore(path, tenant="t1")             # a new process reopens the record
        notes = cs2.list_notes()
        assert len(notes) == 1 and notes[0]["signed_by"] == "dr_giatros" and notes[0]["signed_at"]
        cs2.kv.close()
        print("✅ clinical record: a signed note SURVIVES restart (persisted, timestamped, attributed)")
    finally:
        os.remove(path)


def main():
    test_keyless_dispatch_queues_honestly()
    test_empty_contact_blocked()
    test_autopilot_approval_flows_through_channels()
    test_checkin_arrived_then_completed()
    test_signed_notes_persist_across_restart()
    print("\n🎯 CHANNELS + CHECK-IN + PERSISTED NOTES: ALL TESTS PASSED — plug keys and it sends")


if __name__ == "__main__":
    main()
