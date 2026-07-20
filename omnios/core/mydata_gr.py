"""
mydata_gr.py — Ελληνικό e-τιμολόγιο: AADE myDATA adapter (νομική υποχρέωση κάθε ιατρείου).
==========================================================================================
Χτίζει το επίσημο SendInvoices XML από ένα τιμολόγιο του ClinicStore και το υποβάλλει στο
myDATA REST API της ΑΑΔΕ με τα credentials ΤΟΥ ΓΙΑΤΡΟΥ (aade-user-id + subscription key από το
mydata.aade.gr) — εμείς είμαστε το λογισμικό, ο γιατρός ο υπόχρεος (δες legal/GTM 19/6).

Ελληνικά δεδομένα:
  • Ιατρικές υπηρεσίες B2C → τύπος παραστατικού 11.2 (ΑΠΥ — Απόδειξη Παροχής Υπηρεσιών Λιανικής).
  • ΦΠΑ: απαλλαγή ιατρικών υπηρεσιών (άρθρο 22 §1 ιδ΄ Ν.2859/2000) → vatCategory 7 (0%),
    vatExemptionCategory 15 («Χωρίς ΦΠΑ – άρθρο 22»).
  • Η ΑΑΔΕ επιστρέφει MARK (Μοναδικός Αριθμός Καταχώρησης) — αποθηκεύεται πάνω στο τιμολόγιο.

ΤΙΜΙΟΤΗΤΑ (fail-honest):
  • ΧΩΡΙΣ credentials (MYDATA_USER_ID + MYDATA_SUBSCRIPTION_KEY) → το XML χτίζεται & validate-άρεται,
    το τιμολόγιο μαρκάρεται «queued» με τον λόγο — ΠΟΤΕ δεν ισχυριζόμαστε υποβολή που δεν έγινε.
  • Πρώτη ζωντανή υποβολή = ΜΟΝΟ εποπτευόμενα, με το AADE key του γιατρού, στο sandbox πρώτα
    (mydata-dev). Readiness: L1 (payload+queue tested)· L2 μετά από sandbox round-trip.

Deterministic, stdlib-only (xml.etree + urllib). Tests: python core/test_mydata_gr.py
"""
from __future__ import annotations

import os
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Optional

_PROD_URL = "https://mydatapi.aade.gr/myDATA/SendInvoices"
_DEV_URL = "https://mydataapidev.aade.gr/SendInvoices"


def _creds() -> tuple[str, str]:
    return (os.environ.get("MYDATA_USER_ID", "").strip(),
            os.environ.get("MYDATA_SUBSCRIPTION_KEY", "").strip())


def build_invoice_xml(invoice: dict, clinic: dict, series: str = "A", aa: int = 1,
                      issue_date: Optional[str] = None) -> str:
    """SendInvoices XML για ΑΠΥ ιατρικών υπηρεσιών (11.2, ΦΠΑ 0% άρθρο 22).
    `clinic` χρειάζεται: afm (ΑΦΜ γιατρού), branch (0=έδρα). `invoice`: amount, id."""
    afm = str(clinic.get("afm", "")).strip()
    if not (afm.isdigit() and len(afm) == 9):
        raise ValueError(f"invalid_afm: το ΑΦΜ του ιατρείου πρέπει να είναι 9 ψηφία (έχει: {afm!r})")
    amount = round(float(invoice["amount"]), 2)
    if amount <= 0:
        raise ValueError("invalid_amount: το ποσό πρέπει να είναι > 0")
    from clinic_store import today_iso
    date = issue_date or today_iso()

    NS = "http://www.aade.gr/myDATA/invoice/v1.0"
    ET.register_namespace("", NS)
    root = ET.Element(f"{{{NS}}}InvoicesDoc")
    inv = ET.SubElement(root, f"{{{NS}}}invoice")

    issuer = ET.SubElement(inv, f"{{{NS}}}issuer")
    ET.SubElement(issuer, f"{{{NS}}}vatNumber").text = afm
    ET.SubElement(issuer, f"{{{NS}}}country").text = "GR"
    ET.SubElement(issuer, f"{{{NS}}}branch").text = str(clinic.get("branch", 0))

    hdr = ET.SubElement(inv, f"{{{NS}}}invoiceHeader")
    ET.SubElement(hdr, f"{{{NS}}}series").text = series
    ET.SubElement(hdr, f"{{{NS}}}aa").text = str(aa)
    ET.SubElement(hdr, f"{{{NS}}}issueDate").text = date
    ET.SubElement(hdr, f"{{{NS}}}invoiceType").text = "11.2"   # ΑΠΥ λιανικής
    ET.SubElement(hdr, f"{{{NS}}}currency").text = "EUR"

    det = ET.SubElement(inv, f"{{{NS}}}invoiceDetails")
    ET.SubElement(det, f"{{{NS}}}lineNumber").text = "1"
    ET.SubElement(det, f"{{{NS}}}netValue").text = f"{amount:.2f}"
    ET.SubElement(det, f"{{{NS}}}vatCategory").text = "7"              # 0%
    ET.SubElement(det, f"{{{NS}}}vatAmount").text = "0.00"
    ET.SubElement(det, f"{{{NS}}}vatExemptionCategory").text = "15"    # άρθρο 22 (ιατρικές υπηρεσίες)
    cls = ET.SubElement(det, f"{{{NS}}}incomeClassification")
    ET.SubElement(cls, f"{{{NS}}}classificationType").text = "E3_561_003"  # λιανικές πωλήσεις υπηρεσιών
    ET.SubElement(cls, f"{{{NS}}}classificationCategory").text = "category1_3"
    ET.SubElement(cls, f"{{{NS}}}amount").text = f"{amount:.2f}"

    summ = ET.SubElement(inv, f"{{{NS}}}invoiceSummary")
    ET.SubElement(summ, f"{{{NS}}}totalNetValue").text = f"{amount:.2f}"
    ET.SubElement(summ, f"{{{NS}}}totalVatAmount").text = "0.00"
    ET.SubElement(summ, f"{{{NS}}}totalWithheldAmount").text = "0.00"
    ET.SubElement(summ, f"{{{NS}}}totalFeesAmount").text = "0.00"
    ET.SubElement(summ, f"{{{NS}}}totalStampDutyAmount").text = "0.00"
    ET.SubElement(summ, f"{{{NS}}}totalOtherTaxesAmount").text = "0.00"
    ET.SubElement(summ, f"{{{NS}}}totalDeductionsAmount").text = "0.00"
    ET.SubElement(summ, f"{{{NS}}}totalGrossValue").text = f"{amount:.2f}"

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def submit_invoice(store, iid: str, clinic_fiscal: dict, sandbox: bool = True) -> dict[str, Any]:
    """Υποβολή ΕΝΟΣ τιμολογίου στο myDATA. Fail-honest: χωρίς credentials → queued με λόγο.
    Επιστρέφει {status, mark?, reason?, xml_bytes}. Το MARK αποθηκεύεται πάνω στο invoice."""
    inv = next((i for i in store.list_invoices() if i["id"] == iid), None)
    if not inv:
        raise KeyError(iid)
    if inv.get("mydata_mark"):
        return {"status": "already_submitted", "mark": inv["mydata_mark"]}   # idempotent

    xml_payload = build_invoice_xml(inv, clinic_fiscal)   # validate-άρει ΑΦΜ/ποσό ακόμα κι αν μπει σε ουρά
    user_id, sub_key = _creds()
    if not (user_id and sub_key):
        store.kv.put(store._ns("invoice"), iid, {**inv, "mydata_status": "queued"})
        store._audit("mydata.queued", {"invoice": iid, "reason": "no_credentials"})
        return {"status": "queued",
                "reason": "Λείπουν MYDATA_USER_ID + MYDATA_SUBSCRIPTION_KEY (τα βγάζει ο γιατρός στο mydata.aade.gr)",
                "xml_chars": len(xml_payload)}

    url = _DEV_URL if sandbox else _PROD_URL
    req = urllib.request.Request(url, data=xml_payload.encode("utf-8"), method="POST",
                                 headers={"aade-user-id": user_id,
                                          "ocp-apim-subscription-key": sub_key,
                                          "Content-Type": "application/xml"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as exc:
        # ακριβές error για copy-paste debugging (βλ. debug_log pattern)
        detail = ""
        if hasattr(exc, "read"):
            try:
                detail = exc.read().decode("utf-8", "replace")[:800]
            except Exception:
                pass
        store._audit("mydata.error", {"invoice": iid, "error": f"{type(exc).__name__}: {exc}"})
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}", "detail": detail}

    mark = ""
    try:
        resp = ET.fromstring(body)
        mark = next((el.text for el in resp.iter() if el.tag.endswith("invoiceMark") and el.text), "")
    except ET.ParseError:
        pass
    if mark:
        store.kv.put(store._ns("invoice"), iid,
                     {**inv, "mydata_mark": mark, "mydata_status": "submitted",
                      "mydata_env": "sandbox" if sandbox else "production"})
        store._audit("mydata.submitted", {"invoice": iid, "mark": mark, "sandbox": sandbox})
        return {"status": "submitted", "mark": mark, "sandbox": sandbox}
    store._audit("mydata.rejected", {"invoice": iid, "body": body[:400]})
    return {"status": "rejected", "reason": body[:800]}
