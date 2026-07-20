"""
test_risk_defense.py — Clinic Brain → autopilot: high-risk ραντεβού ΠΡΟΛΗΠΤΙΚΑ προστατεύεται.
Run: python core/test_risk_defense.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import datetime as dt

from clinic_store import ClinicStore
from clinic_autopilot import ClinicAutopilot


def _d(n: int) -> str:
    return (dt.date.today() + dt.timedelta(days=n)).isoformat()


def _setup():
    cs = ClinicStore(":memory:")
    cs.setup_clinic(name="Τεστ", default_value=50)
    p = cs.add_patient("Χρόνιος Ασθενής", phone="6970009999", consent=True)
    # ιστορικό: 2 no-show / 3 → high risk στο επόμενο ραντεβού
    cs.add_appointment("09:00", "Χρόνιος Ασθενής", date=_d(-30), status="completed", patient_id=p["id"])
    cs.add_appointment("09:00", "Χρόνιος Ασθενής", date=_d(-20), status="no_show", patient_id=p["id"])
    cs.add_appointment("09:00", "Χρόνιος Ασθενής", date=_d(-10), status="no_show", patient_id=p["id"])
    cs.add_appointment("10:00", "Χρόνιος Ασθενής", date=_d(2), status="confirmed", patient_id=p["id"])
    return cs, ClinicAutopilot(cs), p


def test_sweep_files_risk_defense():
    cs, ap, p = _setup()
    r = ap.sweep()
    rd = [x for x in ap.pending() if x["type"] == "risk_defense"]
    assert rd, f"δεν προτάθηκε risk_defense! pending={[x['type'] for x in ap.pending()]}"
    prop = rd[0]
    assert "no-show" in prop["why"], prop["why"]           # κουβαλάει τους ΛΟΓΟΥΣ του brain
    assert prop["risk"] == "high", "risk_defense πρέπει να είναι high-risk (θέλει έγκριση)"
    print(f"✅ sweep πρότεινε risk_defense · why: «{prop['why'][:80]}…»")
    return cs, ap, prop


def test_approve_executes():
    cs, ap, prop = test_sweep_files_risk_defense()
    out = ap.approve(prop["id"], approver="γιατρός")
    assert out["status"] == "approved" and "sms" in out["executed"], out
    # idempotent: δεύτερο approve δεν ξαναστέλνει
    out2 = ap.approve(prop["id"], approver="γιατρός")
    assert out2["executed"] == out["executed"]
    print(f"✅ approve → εκτελέστηκε ({out['executed']}) · idempotent στο 2ο κλικ")


def test_consent_blocks():
    cs, ap, p = _setup()
    cs.update_patient(p["id"], consent=False)          # ο ασθενής ανακαλεί συναίνεση
    ap.sweep()
    rd = [x for x in ap.pending() if x["type"] == "risk_defense"]
    assert not rd, "risk_defense προτάθηκε ΧΩΡΙΣ συναίνεση!"
    print("✅ consent=False → ΚΑΜΙΑ πρόταση επικοινωνίας (GDPR Art.9 gate)")


def main():
    test_approve_executes()
    test_consent_blocks()
    print("\n🛡️ RISK DEFENSE: ALL TESTS PASSED — το brain τροφοδοτεί τον autopilot, governed + consent-gated")


if __name__ == "__main__":
    main()
