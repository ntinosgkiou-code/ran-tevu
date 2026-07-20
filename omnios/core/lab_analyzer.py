"""
lab_analyzer.py — OFFLINE ανάλυση εργαστηριακών εξετάσεων (PDF/κείμενο) — 0 bytes egress.
=========================================================================================
Το gap της ημέρας του γιατρού: ο ασθενής φέρνει PDF αιματολογικών· ο γιατρός ψάχνει χειροκίνητα
τις τιμές εκτός ορίων. Αυτό το module τις απομονώνει ΤΟΠΙΚΑ (deterministic parsing, κανένα cloud
call — το «100% τοπικά» pitch είναι κυριολεκτικό εδώ) και ενημερώνει την καρτέλα ως DRAFT σημείωση.

Τιμιότητα / όρια (SaMD firewall — ίδιο με τον scribe):
  • ΔΕΝ διαγιγνώσκει. Συγκρίνει αριθμούς με γενικά reference ranges ενηλίκων και ΕΠΙΣΗΜΑΙΝΕΙ.
  • Τα όρια του εκάστοτε εργαστηρίου (αν βρεθούν στο PDF) ΥΠΕΡΙΣΧΥΟΥΝ των defaults.
  • Ό,τι δεν αναγνωρίζεται αναφέρεται ως «μη αναγνωσμένο» — ποτέ δεν μαντεύει.
  • Output = DRAFT στην καρτέλα· ο γιατρός επιβεβαιώνει.

Deterministic, stdlib (+pypdf προαιρετικά για PDF bytes). Tests: python core/test_lab_analyzer.py
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Γενικά reference ranges ενηλίκων (screening defaults — του εργαστηρίου υπερισχύουν αν βρεθούν).
# (marker, [ελληνικά/λατινικά synonyms], unit, low, high)
_MARKERS = [
    ("Αιμοσφαιρίνη", ["αιμοσφαιρίνη", "hgb", "hb", "hemoglobin"], "g/dL", 12.0, 17.5),
    ("Αιματοκρίτης", ["αιματοκρίτης", "hct", "hematocrit"], "%", 36.0, 52.0),
    ("Σάκχαρο", ["σάκχαρο", "γλυκόζη", "glu", "glucose"], "mg/dL", 70.0, 100.0),
    ("Χοληστερόλη ολική", ["χοληστερόλη", "χοληστερίνη", "chol", "cholesterol"], "mg/dL", 0.0, 200.0),
    ("HDL", ["hdl"], "mg/dL", 40.0, 100.0),
    ("LDL", ["ldl"], "mg/dL", 0.0, 130.0),
    ("Τριγλυκερίδια", ["τριγλυκερίδια", "trig", "triglycerides"], "mg/dL", 0.0, 150.0),
    ("TSH", ["tsh"], "μIU/mL", 0.4, 4.0),
    ("Κρεατινίνη", ["κρεατινίνη", "crea", "creatinine"], "mg/dL", 0.6, 1.3),
    ("Φερριτίνη", ["φερριτίνη", "ferritin"], "ng/mL", 20.0, 300.0),
    ("Σίδηρος", ["σίδηρος", "fe ", "iron"], "μg/dL", 50.0, 170.0),
    ("Β12", ["b12", "β12"], "pg/mL", 200.0, 900.0),
]
_NUM = r"([0-9]+(?:[.,][0-9]+)?)"
# προαιρετικό range δίπλα στην τιμή, π.χ. «(70-100)» ή «Φ.Τ. 70 - 100» — τα όρια ΤΟΥ εργαστηρίου
_RANGE = re.compile(_NUM + r"\s*[-–έως]+\s*" + _NUM)


def _to_f(s: str) -> float:
    return float(s.replace(",", "."))


def analyze_text(text: str) -> dict[str, Any]:
    """Εξάγει δείκτες από κείμενο εξετάσεων· επισημαίνει εκτός-ορίων με κατεύθυνση + όρια."""
    findings, seen = [], set()
    for line in text.splitlines():
        low_line = line.lower()
        for name, syns, unit, lo, hi in _MARKERS:
            if name in seen or not any(s in low_line for s in syns):
                continue
            m = re.search(_NUM, re.sub(r"(?i)" + "|".join(map(re.escape, syns)), " ", low_line, count=1))
            if not m:
                continue
            val = _to_f(m.group(1))
            # όρια εργαστηρίου στην ίδια γραμμή; (π.χ. «70-100») → υπερισχύουν των defaults
            rest = low_line[m.end():]
            rm = _RANGE.search(rest)
            use_lo, use_hi, src = (lo, hi, "default")
            if rm:
                r1, r2 = _to_f(rm.group(1)), _to_f(rm.group(2))
                if r1 < r2 and (r1 or r2):
                    use_lo, use_hi, src = r1, r2, "εργαστηρίου"
            status = "low" if val < use_lo else "high" if val > use_hi else "normal"
            findings.append({"marker": name, "value": val, "unit": unit,
                             "range": f"{use_lo:g}–{use_hi:g}", "range_source": src,
                             "status": status})
            seen.add(name)
    flagged = [f for f in findings if f["status"] != "normal"]
    return {"markers_found": len(findings), "flagged": flagged, "all": findings,
            "summary": ("Όλες οι αναγνωσμένες τιμές εντός ορίων."
                        if findings and not flagged else
                        f"{len(flagged)} τιμές εκτός ορίων: " +
                        "; ".join(f"{f['marker']} {f['value']:g} {f['unit']} "
                                  f"({'↑' if f['status'] == 'high' else '↓'} όρια {f['range']})"
                                  for f in flagged) if flagged else
                        "Δεν αναγνωρίστηκαν εργαστηριακοί δείκτες στο κείμενο."),
            "disclaimer": ("Αυτόματη τοπική εξαγωγή τιμών (0 bytes egress). ΔΕΝ αποτελεί διάγνωση — "
                           "τα ευρήματα επιβεβαιώνονται από τον θεράποντα ιατρό.")}


def analyze_pdf_bytes(data: bytes) -> dict[str, Any]:
    """PDF → κείμενο (pypdf, τοπικά) → analyze_text. Σκαναρισμένα PDF χωρίς text layer → honest μήνυμα."""
    try:
        import io
        from pypdf import PdfReader
        text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    except Exception as exc:
        return {"markers_found": 0, "flagged": [], "all": [],
                "summary": f"Αδυναμία ανάγνωσης PDF ({type(exc).__name__}) — δώσε το ως κείμενο.",
                "disclaimer": ""}
    if not text.strip():
        return {"markers_found": 0, "flagged": [], "all": [],
                "summary": "Το PDF δεν έχει text layer (σκαναρισμένο;) — χρειάζεται OCR ή πληκτρολόγηση.",
                "disclaimer": ""}
    return analyze_text(text)


def analyze_and_chart(store, patient: str, text: Optional[str] = None,
                      pdf_bytes: Optional[bytes] = None) -> dict[str, Any]:
    """Αναλύει + γράφει DRAFT σημείωση στην καρτέλα του ασθενή (ο γιατρός επιβεβαιώνει)."""
    res = analyze_pdf_bytes(pdf_bytes) if pdf_bytes else analyze_text(text or "")
    if res["markers_found"]:
        note = store.add_note({"patient": patient, "kind": "lab_results_draft",
                               "body": res["summary"], "signed_by": "",
                               "flagged": len(res["flagged"])})
        res["note_id"] = note["id"]
        store._audit("labs.analyzed_local", {"patient_ref": note["id"],
                                             "markers": res["markers_found"],
                                             "flagged": len(res["flagged"]), "bytes_egress": 0})
    return res
