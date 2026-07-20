"""
test_api_deeptech.py — tests for the LIVE deep-tech endpoints the product UI calls.
These prove the browser talks to the REAL algorithms (edge PII, tamper-evident audit,
the closed agentic loop, governed tool-calling) — nothing is faked client-side.
Run: python core/test_api_deeptech.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastapi.testclient import TestClient
from api import app, _DEV_API_KEY

client = TestClient(app, headers={"X-API-Key": _DEV_API_KEY})
anon = TestClient(app)   # no key — ONLY the stateless showcase endpoints stay public
                         # (tamper-demo/merkle/conformal/thompson); all data endpoints are keyed

PII = ("Είμαι ο Γιώργος Παπαδόπουλος, ΑΜΚΑ 12036512345, κάρτα 4539 1488 0343 6467, "
       "IBAN GR1601101250000000012300695, τηλ 6945123456, email g.p@clinic.gr")


def test_anonymize_is_real_and_reversible():
    r = client.post("/anonymize", json={"text": PII})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["pii_count"] >= 5, f"expected ≥5 PII, got {b['pii_count']}"
    assert b["reversible"] is True, "the local token→value map must restore the original exactly"
    assert b["bytes_egress"] == 0, "nothing may leave the network"
    # raw PII must NOT appear in the redacted text or the masked previews
    assert "12036512345" not in b["redacted"] and "6945123456" not in b["redacted"]
    for e in b["entities"]:
        assert "[REDACTED_" in e["token"] and "•" in e["preview"]
    kinds = {e["kind"] for e in b["entities"]}
    assert {"AMKA", "IBAN", "CARD", "PHONE", "EMAIL"} <= kinds, f"missing detectors: {kinds}"
    print(f"✅ POST /anonymize → {b['pii_count']} PII tokenized locally, reversible, 0 bytes out ({sorted(kinds)})")


def test_anonymize_validators_reject_false_positives():
    # a 16-digit run that FAILS Luhn (valid card 4539…6467 with the check digit broken) → not a card
    r = client.post("/anonymize", json={"text": "Ο κωδικός παραγγελίας είναι 4539148803436460 ok"})
    b = r.json()
    cards = [e for e in b["entities"] if e["kind"] == "CARD"]
    assert not cards, "Luhn-invalid number wrongly flagged as a card"
    print("✅ POST /anonymize → validators (Luhn/mod-97) reject false positives")


def test_audit_chain_and_verify():
    client.post("/anonymize", json={"text": PII})   # generate at least one real record
    r = client.get("/audit")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["intact"] is True and b["count"] >= 1
    assert b["chain"][0]["prev"] == "genesis" or b["chain"][-1]["hash"], "chain must carry hash links"
    v = client.post("/audit/verify")
    assert v.json()["intact"] is True
    print(f"✅ GET /audit → {b['count']} records, chain intact; POST /audit/verify → intact")


def test_audit_tamper_is_detected():
    r = anon.post("/audit/tamper-demo")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["before"] is True, "a clean chain must verify"
    assert b["after"] is False, "editing a past record MUST break the chain"
    assert b["detected_at_seq"] == b["tampered_seq"], "the break must be pinpointed to the forged record"
    # and the real audit log is untouched
    assert client.post("/audit/verify").json()["intact"] is True
    print(f"✅ POST /audit/tamper-demo → forgery detected at seq #{b['detected_at_seq']} (real chain untouched)")


def test_agentic_loop_learns_and_recovers():
    r = client.post("/agentic/run", json={"appointments": 3, "appointment_value": 45})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["start_no_show_prob"] == 0.23
    assert b["learned_no_show_prob"] > b["start_no_show_prob"], "learner must move toward the true ~0.5"
    assert b["observations"] >= 30 and b["recovered_eur"] > 0
    assert b["audit_chain_intact"] is True and len(b["trace"]) >= 1
    print(f"✅ POST /agentic/run → learned {b['start_no_show_prob']}→{b['learned_no_show_prob']} "
          f"(true 0.5), €{b['recovered_eur']} recovered, chain intact")


def test_ledger_and_metrics():
    lg = client.get("/ledger").json()
    assert lg["billing"]["roi"] == 10.0 and lg["billing"]["total_value_eur"] > 0
    m = client.get("/metrics").json()
    assert m["audit_intact"] is True and m["recovered_eur"] > 0 and m["bytes_egress"] == 0
    print(f"✅ GET /ledger → €{lg['billing']['total_value_eur']} value, ROI {lg['billing']['roi']}x · GET /metrics live")


def test_governed_tool_calling_hitl():
    # high-risk SMS: propose → PENDING_APPROVAL (sandbox preview, no send) → human approves → EXECUTED
    p = client.post("/govern/propose", json={
        "capability": "send_recovery_sms",
        "params": {"to_token": "[REDACTED_PHONE_1]", "text": "Χάσαμε το ραντεβού σας — να το ξανακλείσουμε;"},
        "reason": "no-show recovery"})
    assert p.status_code == 200, p.text
    pb = p.json()
    assert pb["status"] == "PENDING_APPROVAL" and pb["evidence"]["commits"] is False
    a = client.post("/govern/approve", json={"proposal_id": pb["id"], "approver": "dr_papadopoulos"})
    ab = a.json()
    assert ab["status"] == "EXECUTED" and ab["execution_id"]
    # low-risk booking auto-executes without approval
    lo = client.post("/govern/propose", json={"capability": "book_calendar_slot",
                                              "params": {"slot": "αύριο 17:00"}, "reason": "chat booking"})
    assert lo.json()["status"] == "EXECUTED"
    # keyed, read-only preview shows routing WITHOUT executing
    pv = client.post("/govern/preview", json={"capability": "send_recovery_sms",
                                              "params": {"to_token": "[REDACTED_PHONE_1]", "text": "hi"}})
    assert pv.status_code == 200 and pv.json()["status"] == "PREVIEW"
    assert pv.json()["evidence"]["commits"] is False and "PENDING_APPROVAL" in pv.json()["would_route_to"]
    print("✅ /govern: preview(keyed,no-commit) · high-risk SMS → HITL → EXECUTED · low-risk auto-executed")


def test_govern_requires_auth():
    r = anon.post("/govern/propose", json={"capability": "send_recovery_sms", "params": {}})
    assert r.status_code == 401, "acting endpoints must require an API key"
    print("✅ /govern/propose rejects missing API key (401)")


def test_public_surface_is_locked_down():
    """SECURITY CONTRACT: every data/compute endpoint is keyed; only /health, /version and the
    four stateless showcase endpoints (tamper-demo, merkle, conformal, thompson) stay public."""
    gated_get = ["/audit", "/explanations", "/ledger", "/metrics", "/channels/status",
                 "/compliance/pending", "/compliance/claim/x/fhir"]
    for path in gated_get:
        assert anon.get(path).status_code == 401, f"GET {path} must 401 without a key"
    gated_post = [("/anonymize", {"text": "x"}), ("/govern/preview", {"capability": "c", "params": {}}),
                  ("/agentic/run", {"appointments": 1, "appointment_value": 45}),
                  ("/audit/verify", {})]
    for path, body in gated_post:
        assert anon.post(path, json=body).status_code == 401, f"POST {path} must 401 without a key"
    for path in ["/health", "/version", "/audit/merkle", "/compliance/conformal"]:
        assert anon.get(path).status_code == 200, f"GET {path} must stay public"
    assert anon.post("/audit/tamper-demo").status_code == 200, "tamper-demo (throwaway chain) stays public"
    assert anon.post("/agentic/thompson", json={}).status_code == 200, "thompson (pure simulation) stays public"
    hb = anon.get("/health").json()
    assert not isinstance(hb["channels"], dict), "/health must not key channel modes by tenant slug"
    print(f"✅ public surface locked: {len(gated_get) + len(gated_post)} data endpoints 401 anon · "
          f"6 stateless showcase endpoints public · no tenant slugs on /health")


def main():
    test_anonymize_is_real_and_reversible()
    test_anonymize_validators_reject_false_positives()
    test_audit_chain_and_verify()
    test_audit_tamper_is_detected()
    test_agentic_loop_learns_and_recovers()
    test_ledger_and_metrics()
    test_governed_tool_calling_hitl()
    test_govern_requires_auth()
    test_public_surface_is_locked_down()
    print("\n🎯 LIVE DEEP-TECH ENDPOINTS: ALL TESTS PASSED — the browser calls the real algorithms")


if __name__ == "__main__":
    main()
