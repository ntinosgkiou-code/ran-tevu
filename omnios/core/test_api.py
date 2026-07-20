"""
test_api.py — PRODUCTION API tests (FastAPI TestClient, in-process, no server needed).
Run: python core/test_api.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastapi.testclient import TestClient
from api import app, _DEV_API_KEY

client = TestClient(app, headers={"X-API-Key": _DEV_API_KEY})   # all acting endpoints require a key

KB = ("Ασθενής με ΑΜΚΑ 12036512345. Η ασπιρίνη μειώνει τον πυρετό. "
      "Η ασπιρίνη αντενδείκνυται για έλκος. Το έλκος προκαλεί πόνο στο στομάχι.")


def test_auth_required_on_acting_endpoints():
    anon = TestClient(app)   # no API key
    assert anon.get("/health").status_code == 200, "health must stay public"
    for path, body in [("/ingest", {"text": "x"}), ("/ask", {"query": "x"}),
                       ("/act", {"appointment_value": 45}), ("/route", {"message": "x"})]:
        assert anon.post(path, json=body).status_code == 401, f"{path} must require an API key"
    print("✅ auth: acting endpoints reject missing key (401); /health stays public")


def test_health():
    r = client.get("/health")
    b = r.json()
    assert r.status_code == 200 and b["status"] == "ok"
    assert len(b["components"]) >= 6 and b["version"] and b["clinics"] >= 1
    assert "audit_intact" in b and "uptime_seconds" in b
    print(f"✅ GET /health → ok, v{b['version']}, {len(b['components'])} components, {b['clinics']} clinics")


def test_ingest_strips_pii():
    r = client.post("/ingest", json={"text": KB, "source": "kb"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pii_removed"] >= 1 and body["triplets"] >= 3
    print(f"✅ POST /ingest → {body['pii_removed']} PII stripped, {body['triplets']} triplets")


def test_ask_grounded():
    client.post("/ingest", json={"text": KB})
    r = client.post("/ask", json={"query": "ασπιρίνη πυρετό"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pii_protected"] and body["confidence"] >= 0.9 and "μειώνει" in body["answer"]
    assert len(body["transcript"]) >= 3
    print(f"✅ POST /ask → grounded answer, conf={body['confidence']}, hitl={body['hitl_required']}")


def test_act_and_bill():
    ra = client.post("/act", json={"appointment_value": 45, "base_no_show_prob": 0.30})
    assert ra.status_code == 200 and ra.json()["recommended"] != "none"
    rb = client.post("/bill", json={"events": [{"kind": "recovered_appointment", "euros": 45},
                                               {"kind": "filled_slot", "euros": 40}]})
    assert rb.status_code == 200 and rb.json()["charge_eur"] == 8.5 and rb.json()["roi"] == 10.0
    print(f"✅ POST /act → '{ra.json()['recommended']}' · POST /bill → €{rb.json()['charge_eur']} (ROI {rb.json()['roi']}x)")


def test_validation_error():
    r = client.post("/ingest", json={"text": ""})     # min_length=1 violation
    assert r.status_code == 422, "empty text must be rejected by validation"
    print("✅ validation: empty ingest rejected with 422")


def test_scribe_endpoints():
    transcript = "Ασθενής με ΑΜΚΑ 12036512345. Παράπονο: οσφυαλγία μετά από άρση βάρους."
    d = client.post("/scribe/draft", json={"transcript": transcript})
    assert d.status_code == 200 and d.json()["status"] == "DRAFT", d.text
    assert "12036512345" not in d.json()["transcript_redacted"], "PII in drafted note"
    did = d.json()["id"]
    bad = client.post("/scribe/sign", json={"draft_id": did, "clinician_id": ""})   # empty → validation 422
    assert bad.status_code == 422
    s = client.post("/scribe/sign", json={"draft_id": did, "clinician_id": "dr_9"})
    assert s.status_code == 200 and s.json()["status"] == "SIGNED" and s.json()["signed_by"] == "dr_9"
    print("✅ POST /scribe/draft → DRAFT (PII stripped) · /scribe/sign → SIGNED (clinician required)")


def test_route_single_front_door():
    # `with` triggers the lifespan → booking actors live for the app's lifetime (persist across requests)
    with TestClient(app, headers={"X-API-Key": _DEV_API_KEY}) as c:
        c.post("/ingest", json={"text": KB})
        rk = c.post("/route", json={"message": "τι σχέση έχει η ασπιρίνη με τον πόνο στο στομάχι;"})
        assert rk.status_code == 200 and rk.json()["intent"] == "knowledge", rk.text
        rb = c.post("/route", json={"message": "Θέλω να κλείσω ραντεβού αύριο στις 5", "sender": "306945000000"})
        assert rb.json()["intent"] == "booking"
        assert rb.json()["result"]["governance"]["back_office_pii_access"] is False, "PII must stay isolated via /route"
        rbill = c.post("/route", json={"message": "πόσα μας γλίτωσε;",
                                       "value_events": [{"kind": "recovered", "euros": 45}]})
        assert rbill.json()["intent"] == "billing" and rbill.json()["result"]["charge_eur"] == 4.5
    print("✅ POST /route: one front door → knowledge / booking (PII isolated) / billing")


def main():
    test_auth_required_on_acting_endpoints()
    test_health()
    test_ingest_strips_pii()
    test_ask_grounded()
    test_act_and_bill()
    test_validation_error()
    test_scribe_endpoints()
    test_route_single_front_door()
    print("\n🎯 PRODUCTION API — FastAPI Medical Intelligence Layer: ALL TESTS PASSED")


if __name__ == "__main__":
    main()
