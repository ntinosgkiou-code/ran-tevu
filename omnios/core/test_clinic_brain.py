"""
test_clinic_brain.py — εξηγήσιμο no-show risk: flag-άρει τον σωστό ασθενή, με ΛΟΓΟΥΣ.
Run: python core/test_clinic_brain.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import datetime as dt

from clinic_store import ClinicStore
from clinic_brain import score_appointment, scan_clinic


def _d(n: int) -> str:
    return (dt.date.today() + dt.timedelta(days=n)).isoformat()


def _seed():
    cs = ClinicStore(":memory:")
    cs.setup_clinic(name="Ιατρείο Τεστ", default_value=50)
    # Χρόνιος no-shower: 2 no_show + 1 completed στο παρελθόν
    cs.add_appointment("09:00", "Χρόνιος", date=_d(-30), status="completed")
    cs.add_appointment("09:00", "Χρόνιος", date=_d(-20), status="no_show")
    cs.add_appointment("09:00", "Χρόνιος", date=_d(-10), status="no_show")
    up_chronic = cs.add_appointment("10:00", "Χρόνιος", date=_d(2), status="confirmed")
    # Συνεπής: 4 completed
    for i, dd in enumerate([-40, -30, -20, -12]):
        cs.add_appointment(f"1{i}:00", "Συνεπής", date=_d(dd), status="completed")
    up_reliable = cs.add_appointment("11:00", "Συνεπής", date=_d(2), status="confirmed")
    # Νέος: χωρίς ιστορικό
    up_new = cs.add_appointment("12:00", "Νέος", date=_d(3), status="confirmed")
    return cs, up_chronic, up_reliable, up_new


def test_bands_and_reasons():
    cs, chronic, reliable, new = _seed()
    sc = score_appointment(cs, chronic)
    sr = score_appointment(cs, reliable)
    sn = score_appointment(cs, new)
    assert sc["band"] == "high", sc
    assert any("no-show" in r for r in sc["reasons"]), sc["reasons"]
    assert "waitlist" in sc["action"], sc
    assert sr["band"] == "low", sr
    assert sn["band"] == "medium", sn
    assert any("νέος" in r for r in sn["reasons"]), sn["reasons"]
    print(f"✅ Χρόνιος → {sc['band']} (risk {sc['risk']}) · λόγοι: {sc['reasons']}")
    print(f"✅ Συνεπής → {sr['band']} (risk {sr['risk']})")
    print(f"✅ Νέος → {sn['band']} (risk {sn['risk']}) · λόγοι: {sn['reasons']}")


def test_scan_prioritises():
    cs, *_ = _seed()
    r = scan_clinic(cs, horizon_days=14)
    assert r["scanned"] == 3 and r["high_risk"] >= 1 and r["at_risk"] >= 2, r
    assert r["appointments"][0]["patient"] == "Χρόνιος", "ο υψηλού ρίσκου πρέπει να είναι πρώτος"
    assert r["eur_at_risk"] > 0
    print(f"✅ scan: {r['scanned']} σκαναρίστηκαν · {r['high_risk']} high · €{r['eur_at_risk']:g} at-risk · "
          f"1ος={r['appointments'][0]['patient']}")


def main():
    test_bands_and_reasons()
    test_scan_prioritises()
    print("\n🧠 CLINIC BRAIN: ALL TESTS PASSED — εξηγήσιμο ρίσκο, σωστό flag, αυτόνομη προτεραιότητα")


if __name__ == "__main__":
    main()
