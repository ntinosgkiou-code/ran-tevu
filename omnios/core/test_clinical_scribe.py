"""
test_clinical_scribe.py — bounded Clinical Scribe tests.
Verifies PII removal, SNOMED/ICD-10 mapping, unmapped-term flagging, and the SaMD firewall:
a draft never enters the record; only a clinician sign() writes it.
Run: python core/test_clinical_scribe.py
"""
import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from clinical_scribe import ClinicalScribe, NoteStatus

TRANSCRIPT = ("Ασθενής Γιώργος Παππάς, ΑΜΚΑ 12036512345. Παράπονο: οσφυαλγία μετά από άρση βάρους. "
              "Αναφέρει και μυϊκό σπασμό. Σύμπτωμα: σπάνιο σύνδρομο Ζ υπό διερεύνηση.")


def test_pii_removed_and_mapped():
    note = ClinicalScribe().draft_sync(TRANSCRIPT)
    assert "12036512345" not in note.transcript_redacted, "ΑΜΚΑ leaked into the note"
    codes = {e.icd10_code for e in note.entities if e.icd10_code}
    assert "M54.5" in codes, f"οσφυαλγία should map to ICD-10 M54.5; got {codes}"
    assert "M62.838" in codes, "μυϊκός σπασμός should map to muscle spasm"
    assert any("Low back pain (M54.5)" == p for p in note.problem_list)
    print(f"✅ mapping: PII stripped, {len(note.problem_list)} SNOMED/ICD-10 problems structured")


def test_unmapped_term_flagged_not_guessed():
    note = ClinicalScribe().draft_sync(TRANSCRIPT)
    flagged = [e for e in note.entities if e.flagged]
    assert any("σύνδρομο" in e.text.lower() for e in flagged), "unmapped condition must be flagged, not coded"
    assert all(e.snomed_code is None for e in flagged), "flagged entities must carry NO guessed code"
    print(f"✅ safety: {len(flagged)} unmapped term(s) flagged for clinician — never guessed a code")


def test_draft_never_enters_record():
    scribe = ClinicalScribe()
    note = scribe.draft_sync(TRANSCRIPT)
    assert note.status == NoteStatus.DRAFT
    assert scribe.record() == [], "a DRAFT must NOT be in the clinical record (SaMD firewall)"
    print("✅ firewall: draft produced but clinical record stays empty until signed")


def test_sign_requires_clinician_then_persists():
    scribe = ClinicalScribe()
    note = scribe.draft_sync(TRANSCRIPT)
    try:
        scribe.sign(note.id, clinician_id="")
        assert False, "signing without a clinician_id must be refused"
    except PermissionError:
        pass
    signed = scribe.sign(note.id, clinician_id="dr_123")
    assert signed.status == NoteStatus.SIGNED and signed.signed_by == "dr_123"
    assert [n.id for n in scribe.record()] == [note.id], "signed note must now be in the record"
    print("✅ sign: refused without clinician; on sign → SIGNED and written to the record")


def test_async_and_empty_guard():
    async def run():
        scribe = ClinicalScribe()
        note = await scribe.draft(TRANSCRIPT)
        assert note.status == NoteStatus.DRAFT
        try:
            scribe.draft_sync("   ")
            assert False, "empty transcript must raise"
        except ValueError:
            pass
    asyncio.run(run())
    print("✅ async draft + empty-transcript guard")


def main():
    test_pii_removed_and_mapped()
    test_unmapped_term_flagged_not_guessed()
    test_draft_never_enters_record()
    test_sign_requires_clinician_then_persists()
    test_async_and_empty_guard()
    print("\n🎯 CLINICAL SCRIBE (bounded / HITL): ALL TESTS PASSED")


if __name__ == "__main__":
    main()
