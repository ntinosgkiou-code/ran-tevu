"""
test_edge_anonymizer.py — Step 1 tests (edge PII anonymization).
Run:  python core/test_edge_anonymizer.py

Covers: structured-PII detection+validation+reversible tokenization, validator rejection of
fake card/IBAN, phone-length guard, dates-off-by-default (booking intact) vs opt-in, name
cue-detection + de-duplication, and the async API round-trip. spaCy is NOT required (use_ner=False).
"""
import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from edge_anonymizer import EdgeAnonymizer, PIIKind


def test_structured_pii_roundtrip():
    az = EdgeAnonymizer(use_ner=False)
    text = ("ΑΜΚΑ 12036512345, IBAN GR1601101250000000012300695, κάρτα 4242 4242 4242 4242, "
            "email a@b.gr, τηλ 6945123456")
    r = az.anonymize_sync(text)
    for leak in ["12036512345", "GR1601101250000000012300695", "4242 4242 4242 4242", "a@b.gr", "6945123456"]:
        assert leak not in r.redacted, f"leaked: {leak}"
    kinds = {e.kind for e in r.entities}
    assert {PIIKind.AMKA, PIIKind.IBAN, PIIKind.CARD, PIIKind.EMAIL, PIIKind.PHONE} <= kinds, kinds
    assert az.deanonymize_sync(r.redacted, r.mapping()) == text, "round-trip mismatch"
    print("✅ structured PII (ΑΜΚΑ/IBAN/card/email/phone) detected, validated, tokenized, restored")


def test_validation_and_guards():
    az = EdgeAnonymizer(use_ner=False)
    text = "iban GR0000000000000000000000000 short 12345 valid 4242424242424242"
    r = az.anonymize_sync(text)
    assert "GR0000000000000000000000000" in r.redacted, "invalid IBAN wrongly redacted"
    assert "12345" in r.redacted, "too-short number wrongly redacted"
    card = [e for e in r.entities if e.original.replace(" ", "") == "4242424242424242"]
    assert card and card[0].kind == PIIKind.CARD, "valid card should classify as CARD, not PHONE"
    print("✅ validation: invalid IBAN (mod-97) + short number left untouched; valid card → CARD")


def test_dates_off_by_default():
    t = "ραντεβού 12/03/2026 στις 5"
    assert "12/03/2026" in EdgeAnonymizer(use_ner=False).anonymize_sync(t).redacted, "date broke booking"
    assert "12/03/2026" not in EdgeAnonymizer(use_ner=False, redact_dates=True).anonymize_sync(t).redacted
    print("✅ dates: preserved by default (booking intact); redacted only when redact_dates=True")


def test_name_cue_and_dedup():
    az = EdgeAnonymizer(use_ner=False)
    t = "Είμαι ο Νίκος Παππάς και λέγομαι Νίκος Παππάς"
    r = az.anonymize_sync(t)
    assert "Νίκος Παππάς" not in r.redacted, "name leaked"
    name_tokens = {e.token for e in r.entities if e.kind == PIIKind.NAME}
    assert len(name_tokens) == 1, f"same name must map to one stable token, got {name_tokens}"
    print("✅ names: cue-detected + de-duplicated to a single reversible token")


async def _async_roundtrip():
    az = EdgeAnonymizer(use_ner=False)
    original = "email x@y.gr τηλ 6945123456"
    r = await az.anonymize(original)
    assert "x@y.gr" not in r.redacted and "6945123456" not in r.redacted
    assert await az.deanonymize(r.redacted, r.mapping()) == original
    print("✅ async API: anonymize / deanonymize round-trip via asyncio.to_thread")


def main():
    test_structured_pii_roundtrip()
    test_validation_and_guards()
    test_dates_off_by_default()
    test_name_cue_and_dedup()
    asyncio.run(_async_roundtrip())
    print("\n🎯 STEP 1 — Edge PII Anonymization Layer: ALL TESTS PASSED")


if __name__ == "__main__":
    main()
