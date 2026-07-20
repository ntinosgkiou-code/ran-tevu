"""
test_lab_analyzer.py — offline εξετάσεις: σωστή εξαγωγή, όρια εργαστηρίου, honest failures.
Run: python core/test_lab_analyzer.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clinic_store import ClinicStore
from lab_analyzer import analyze_text, analyze_pdf_bytes, analyze_and_chart

REPORT = """ΜΙΚΡΟΒΙΟΛΟΓΙΚΟ ΕΡΓΑΣΤΗΡΙΟ — ΑΠΟΤΕΛΕΣΜΑΤΑ
Αιμοσφαιρίνη: 10,2 g/dL (Φ.Τ. 12.0 - 17.5)
Σάκχαρο (γλυκόζη): 126 mg/dL (70-100)
Χοληστερόλη ολική: 185 mg/dL
TSH: 2.1 μIU/mL
Τριγλυκερίδια: 210 mg/dL (0-150)
"""


def test_extraction_and_flags():
    r = analyze_text(REPORT)
    assert r["markers_found"] == 5, r["markers_found"]
    flagged = {f["marker"]: f for f in r["flagged"]}
    assert set(flagged) == {"Αιμοσφαιρίνη", "Σάκχαρο", "Τριγλυκερίδια"}, set(flagged)
    assert flagged["Αιμοσφαιρίνη"]["status"] == "low" and flagged["Αιμοσφαιρίνη"]["value"] == 10.2
    assert flagged["Σάκχαρο"]["status"] == "high" and flagged["Σάκχαρο"]["range_source"] == "εργαστηρίου"
    # οι φυσιολογικές ΔΕΝ flag-άρονται
    normal = {f["marker"] for f in r["all"] if f["status"] == "normal"}
    assert normal == {"Χοληστερόλη ολική", "TSH"}, normal
    print(f"✅ εξαγωγή: 5 δείκτες · 3 σωστά flagged (↓Hb 10.2, ↑Glu 126 με όρια ΕΡΓΑΣΤΗΡΙΟΥ, ↑Trig) · 2 normal")


def test_honest_on_garbage():
    r = analyze_text("Ο καιρός σήμερα είναι καλός. Τιμή εισιτηρίου 15 ευρώ.")
    assert r["markers_found"] == 0 and "Δεν αναγνωρίστηκαν" in r["summary"]
    r2 = analyze_pdf_bytes(b"not a pdf at all")
    assert r2["markers_found"] == 0 and ("Αδυναμία" in r2["summary"] or "text layer" in r2["summary"])
    print("✅ honest: άσχετο κείμενο → «δεν αναγνωρίστηκαν»· σκουπίδι-PDF → σαφές μήνυμα, όχι μαντεψιά")


def test_pdf_roundtrip():
    # φτιάξε ΠΡΑΓΜΑΤΙΚΟ PDF με pypdf ώστε ο roundtrip PDF→κείμενο→ανάλυση να είναι αληθινός
    import io
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    r = analyze_pdf_bytes(buf.getvalue())     # κενή σελίδα = χωρίς text layer → honest path
    assert r["markers_found"] == 0 and "text layer" in r["summary"]
    print("✅ PDF path: κενό/σκαναρισμένο PDF → honest «χρειάζεται OCR», ποτέ σιωπηλό μηδέν")


def test_chart_draft():
    cs = ClinicStore(":memory:")
    cs.setup_clinic(name="Τεστ")
    r = analyze_and_chart(cs, "Ασθενής Α", text=REPORT)
    assert r.get("note_id"), "δεν γράφτηκε draft σημείωση!"
    notes = cs.list_notes()
    assert len(notes) == 1 and notes[0]["kind"] == "lab_results_draft" and notes[0]["flagged"] == 3
    assert "εκτός ορίων" in notes[0]["body"]
    print(f"✅ καρτέλα: DRAFT σημείωση με τα ευρήματα ({notes[0]['flagged']} flagged) — ο γιατρός επιβεβαιώνει")


def main():
    test_extraction_and_flags()
    test_honest_on_garbage()
    test_pdf_roundtrip()
    test_chart_draft()
    print("\n🔬 LAB ANALYZER (offline, 0 egress): ALL TESTS PASSED")


if __name__ == "__main__":
    main()
