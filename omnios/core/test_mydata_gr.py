"""
test_mydata_gr.py — ελληνικό e-τιμολόγιο ΑΑΔΕ: σωστό XML, fail-honest queue, idempotent, gated.
Run: python core/test_mydata_gr.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import os
import xml.etree.ElementTree as ET

from clinic_store import ClinicStore
from mydata_gr import build_invoice_xml, submit_invoice

os.environ.pop("MYDATA_USER_ID", None)
os.environ.pop("MYDATA_SUBSCRIPTION_KEY", None)

FISCAL = {"afm": "123456789", "branch": 0}


def _store():
    cs = ClinicStore(":memory:")
    cs.setup_clinic(name="Ιατρείο Τεστ", default_value=50)
    inv = cs.add_invoice("Ασθενής Α", 50.0, "unpaid")
    return cs, inv


def test_xml_structure():
    cs, inv = _store()
    xml = build_invoice_xml(inv, FISCAL)
    root = ET.fromstring(xml)                      # parse-άρει = well-formed
    txt = {el.tag.split("}")[-1]: (el.text or "") for el in root.iter()}
    assert txt["vatNumber"] == "123456789" and txt["invoiceType"] == "11.2"
    assert txt["vatCategory"] == "7" and txt["vatExemptionCategory"] == "15"   # ΦΠΑ 0% άρθρο 22
    assert txt["totalGrossValue"] == "50.00" and txt["totalVatAmount"] == "0.00"
    print("✅ XML: έγκυρο SendInvoices · ΑΠΥ 11.2 · ΦΠΑ 0% (άρθρο 22) · σύνολα σωστά")


def test_bad_inputs_rejected():
    cs, inv = _store()
    try:
        build_invoice_xml(inv, {"afm": "123"})
        raise AssertionError("δέχτηκε 3ψήφιο ΑΦΜ!")
    except ValueError as e:
        assert "invalid_afm" in str(e)
    try:
        build_invoice_xml({**inv, "amount": 0}, FISCAL)
        raise AssertionError("δέχτηκε μηδενικό ποσό!")
    except ValueError as e:
        assert "invalid_amount" in str(e)
    print("✅ validation: λάθος ΑΦΜ / μηδενικό ποσό → απορρίπτονται με σαφή λόγο")


def test_no_creds_queues_honestly():
    cs, inv = _store()
    r = submit_invoice(cs, inv["id"], FISCAL)
    assert r["status"] == "queued" and "MYDATA_USER_ID" in r["reason"], r
    stored = next(i for i in cs.list_invoices() if i["id"] == inv["id"])
    assert stored.get("mydata_status") == "queued"
    print("✅ fail-honest: χωρίς credentials → queued με τον λόγο (ποτέ ψεύτικη υποβολή)")


def test_idempotent_after_mark():
    cs, inv = _store()
    cs.kv.put(cs._ns("invoice"), inv["id"], {**inv, "mydata_mark": "400001234567890"})
    r = submit_invoice(cs, inv["id"], FISCAL)
    assert r["status"] == "already_submitted" and r["mark"] == "400001234567890"
    print("✅ idempotent: τιμολόγιο με MARK δεν ξανα-υποβάλλεται")


def test_endpoint_gated():
    from fastapi.testclient import TestClient
    from api import app, _DEV_API_KEY
    anon = TestClient(app)
    assert anon.post("/clinic/invoices/x/mydata", json={"afm": "123456789"}).status_code == 401
    keyed = TestClient(app, headers={"X-API-Key": _DEV_API_KEY})
    iid = keyed.post("/clinic/invoices", json={"patient": "Τεστ", "amount": 40}).json()["id"]
    r = keyed.post(f"/clinic/invoices/{iid}/mydata", json={"afm": "123456789"})
    assert r.status_code == 200 and r.json()["status"] in ("queued", "submitted"), r.text
    # 2 στρώματα validation: μήκος → Pydantic 422· 9 μη-ψηφία → δικός μας έλεγχος 400
    assert keyed.post(f"/clinic/invoices/{iid}/mydata", json={"afm": "123"}).status_code == 422
    assert keyed.post(f"/clinic/invoices/{iid}/mydata", json={"afm": "abcdefghi"}).status_code == 400
    print("✅ endpoint: 401 χωρίς κλειδί · 200 queued με κλειδί · 422/400 σε λάθος ΑΦΜ")


def main():
    test_xml_structure()
    test_bad_inputs_rejected()
    test_no_creds_queues_honestly()
    test_idempotent_after_mark()
    test_endpoint_gated()
    print("\n🇬🇷 myDATA: ALL TESTS PASSED — έτοιμο για sandbox round-trip με AADE key γιατρού (L1)")


if __name__ == "__main__":
    main()
