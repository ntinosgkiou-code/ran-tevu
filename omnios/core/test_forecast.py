"""
test_forecast.py — demand forecasting: honest gating, correct math, endpoint auth.
Run: python core/test_forecast.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import datetime as dt

from clinic_store import ClinicStore
from demand_forecast import forecast_clinic


def _seeded_store(days: int = 30) -> ClinicStore:
    """Ρεαλιστικό ιστορικό: περισσότερα ραντεβού Δευ-Παρ, λίγα Σάββατο, κλειστά Κυριακή."""
    cs = ClinicStore(":memory:")
    cs.setup_clinic(name="T", specialty="Φυσικοθεραπεία", default_value=50)
    today = dt.date.today()
    for back in range(days, 0, -1):
        d = today - dt.timedelta(days=back)
        per_day = {5: 2, 6: 0}.get(d.weekday(), 4)   # Sat=2, Sun=0, weekdays=4
        for i in range(per_day):
            cs.add_appointment(f"{9 + i}:00", f"P{back}-{i}", date=d.isoformat(),
                               status="completed", value=50)
    return cs


def test_insufficient_history_is_honest():
    cs = ClinicStore(":memory:"); cs.setup_clinic(name="T", specialty="x")
    r = forecast_clinic(cs)
    assert r["ready"] is False and "ιστορικ" in r["reason"]
    print("✅ χωρίς ιστορικό → ready:false με λόγο (καμία εφεύρεση προβλέψεων)")


def test_forecast_shape_and_math():
    cs = _seeded_store(30)
    r = forecast_clinic(cs, 14)
    assert r["ready"] is True and r["engine"] in ("google-timesfm", "seasonal-statistical")
    assert len(r["days"]) == 14 and r["history_days"] >= 28
    assert r["avg_session_eur"] == 50.0
    # η εποχικότητα πρέπει να πιαστεί: Κυριακές ~0, καθημερινές > Σάββατα
    sundays = [d for d in r["days"] if d["weekday"] == "Κυριακή"]
    weekdays = [d for d in r["days"] if d["weekday"] not in ("Σάββατο", "Κυριακή")]
    assert all(s["expected_bookings"] <= 1 for s in sundays), "Κυριακές έπρεπε να είναι ~0"
    assert all(w["expected_bookings"] >= 2 for w in weekdays), "καθημερινές έπρεπε να είναι υψηλές"
    # έσοδα = κρατήσεις × μέση αξία (συνεπή)
    d0 = r["days"][0]
    assert abs(d0["expected_eur"] - d0["expected_bookings"] * 50) < 1
    assert r["expected_total_eur"] > 0 and r["insight"]
    print(f"✅ σχήμα+μαθηματικά σωστά · engine={r['engine']} · σύνολο €{r['expected_total_eur']:g} "
          f"· έπιασε την εβδομαδιαία εποχικότητα")


def test_endpoint_is_tenant_gated():
    from fastapi.testclient import TestClient
    from api import app, _DEV_API_KEY
    anon = TestClient(app)
    assert anon.get("/clinic/forecast").status_code == 401, "forecast χωρίς κλειδί πρέπει να 401"
    keyed = TestClient(app, headers={"X-API-Key": _DEV_API_KEY})
    r = keyed.get("/clinic/forecast")
    assert r.status_code == 200 and "ready" in r.json()
    print("✅ endpoint: 401 χωρίς κλειδί · 200 με κλειδί · honest ready flag")


def main():
    test_insufficient_history_is_honest()
    test_forecast_shape_and_math()
    test_endpoint_is_tenant_gated()
    print("\n🎯 DEMAND FORECASTING: ALL TESTS PASSED — TimesFM-ready, honest, tenant-gated")


if __name__ == "__main__":
    main()
