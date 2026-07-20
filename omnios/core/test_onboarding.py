"""
test_onboarding.py — self-serve setup «κουμπώνει σε κάθε ιατρείο»: checklist + one-step config.
Run: python core/test_onboarding.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clinic_store import ClinicStore


def test_checklist_tracks_progress():
    cs = ClinicStore(":memory:")
    st0 = cs.get_clinic().onboarding_status()
    assert not st0["complete"], st0
    # ένα βήμα setup γεμίζει τα κενά
    cs.setup_clinic(name="Οδοντιατρείο Χ", specialty="Οδοντιατρική", hours="Δευ–Σαβ 09:00–21:00",
                    default_value=60, afm="123456789",
                    services=[{"name": "Καθαρισμός", "price": 50, "duration": 30},
                              {"name": "Σφράγισμα", "price": 80, "duration": 45}])
    st1 = cs.get_clinic().onboarding_status()
    assert st1["complete"] and st1["progress"] == "4/4", st1
    assert st1["steps"]["υπηρεσίες & τιμές"] and st1["steps"]["ΑΦΜ (για τιμολόγηση)"]
    print(f"✅ checklist: {st0['progress']} → {st1['progress']} μετά από ΕΝΑ βήμα setup")


def test_specialty_agnostic():
    # ίδιο σύστημα κουμπώνει σε ΟΠΟΙΑΔΗΠΟΤΕ ειδικότητα — τα δεδομένα ανά ιατρείο, όχι hardcoded
    for spec, svc in [("Καρδιολογία", "Triplex"), ("Δερματολογία", "Κρυοπηξία"),
                      ("Ψυχιατρική", "Συνεδρία")]:
        cs = ClinicStore(":memory:")
        c = cs.setup_clinic(specialty=spec, services=[{"name": svc, "price": 70, "duration": 30}])
        assert c.specialty == spec and c.services[0]["name"] == svc
    print("✅ specialty-agnostic: καρδιολογία/δερματολογία/ψυχιατρική — κουμπώνει το ίδιο σύστημα")


def test_endpoints():
    from fastapi.testclient import TestClient
    from api import app, _DEV_API_KEY
    anon = TestClient(app)
    assert anon.get("/clinic/onboarding").status_code == 401
    keyed = TestClient(app, headers={"X-API-Key": _DEV_API_KEY})
    r = keyed.post("/clinic/onboard", json={"specialty": "Παιδιατρική", "afm": "999888777",
                                            "services": [{"name": "Εμβόλιο", "price": 30, "duration": 15}]})
    assert r.status_code == 200 and r.json()["onboarding"]["steps"]["ΑΦΜ (για τιμολόγηση)"], r.text
    st = keyed.get("/clinic/onboarding").json()
    assert st["steps"]["υπηρεσίες & τιμές"]
    print("✅ endpoints: /clinic/onboard ρυθμίζει · /clinic/onboarding δείχνει το checklist")


def main():
    test_checklist_tracks_progress()
    test_specialty_agnostic()
    test_endpoints()
    print("\n🔌 ONBOARDING: ALL TESTS PASSED — self-serve, specialty-agnostic, κουμπώνει σε κάθε ιατρείο")


if __name__ == "__main__":
    main()
