"""
clinical_scribe.py — Clinical Scribe Agent (BOUNDED / human-in-the-loop).
========================================================================
Turns a doctor–patient transcript into a STRUCTURED DRAFT note: PII removed at the edge, clinical
terms mapped to SNOMED-CT concepts + an ICD-10 code for billing, organized as a FHIR-shaped
problem list. It SUGGESTS; it never finalizes.

The SaMD / liability firewall (deliberate, load-bearing):
  • It gives NO diagnosis, triage, or dosing of its own — it only structures what was said.
  • Every note is a DRAFT. Nothing reaches the clinical record until a licensed clinician sign()s it.
  • Low-confidence / unmapped terms are FLAGGED for the clinician instead of guessed.

The terminology map here is a small DEMO lexicon (real deployment uses a SNOMED/UMLS terminology
server via the `mapper` hook — same interface, no code change). Async, Pydantic v2, typed.
Tests: python core/test_clinical_scribe.py
"""
from __future__ import annotations

import asyncio
import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from audit import AUDIT
from edge_anonymizer import EdgeAnonymizer

DISCLAIMER = ("Προσχέδιο δομημένο από AI βοηθό. Δεν αποτελεί διάγνωση/ιατρική συμβουλή. "
              "Απαιτείται έλεγχος και υπογραφή από αδειούχο κλινικό πριν καταχωρηθεί.")

# DEMO lexicon (Greek/English synonyms → SNOMED-CT + ICD-10). Production: terminology server.
_LEXICON = [
    {"terms": ["οσφυαλγία", "οσφυαλγια", "πόνος στη μέση", "low back pain"],
     "snomed": ("279039007", "Low back pain"), "icd10": "M54.5"},
    {"terms": ["κεφαλαλγία", "κεφαλαλγια", "πονοκέφαλος", "πονοκέφαλο", "πονοκεφαλο", "headache"],
     "snomed": ("25064002", "Headache"), "icd10": "R51"},
    {"terms": ["πυρετός", "πυρετο", "fever"],
     "snomed": ("386661006", "Fever"), "icd10": "R50.9"},
    {"terms": ["υπέρταση", "υψηλή πίεση", "hypertension"],
     "snomed": ("38341003", "Hypertensive disorder"), "icd10": "I10"},
    {"terms": ["διαβήτης", "diabetes"],
     "snomed": ("73211009", "Diabetes mellitus"), "icd10": "E11.9"},
    {"terms": ["βήχας", "cough"],
     "snomed": ("49727002", "Cough"), "icd10": "R05"},
    {"terms": ["μυϊκός σπασμός", "μυϊκό σπασμό", "μυικός σπασμός", "μυικό σπασμό", "σπασμ", "muscle spasm"],
     "snomed": ("41652007", "Muscle spasm"), "icd10": "M62.838"},
]
_FLAG_CUES = re.compile(r"(?i:διάγνωση|σύμπτωμα|εκτίμηση|παράπονο|complaint|diagnosis)[:\s]+([^.,\n·]+)")


class NoteStatus(str, Enum):
    DRAFT = "DRAFT"
    SIGNED = "SIGNED"


class ClinicalEntity(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str
    snomed_code: Optional[str] = None
    snomed_display: Optional[str] = None
    icd10_code: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    flagged: bool = False       # True → clinician must resolve (unmapped / low-confidence)


class ScribeNote(BaseModel):
    id: str
    status: NoteStatus
    transcript_redacted: str
    entities: list[ClinicalEntity]
    problem_list: list[str]      # "SNOMED display (ICD-10)" — the FHIR-mirrored Condition summary
    pii_removed: int
    disclaimer: str = DISCLAIMER
    signed_by: Optional[str] = None


def to_soap_gr(note: ScribeNote) -> dict[str, str]:
    """Δομεί το draft σε ελληνικό SOAP (Υποκειμενικά/Αντικειμενικά/Εκτίμηση/Πλάνο) — έτσι γράφουν
    οι Έλληνες κλινικοί. ΔΟΜΕΙ ΜΟΝΟ ό,τι ειπώθηκε (δεν εφευρίσκει ευρήματα/vitals)· ό,τι είναι
    αβέβαιο πάει ρητά στο Πλάνο ως «προς αξιολόγηση». Draft — ισχύει ό,τι και για τη note."""
    mapped = [e for e in note.entities if e.snomed_code and not e.flagged]
    flagged = [e for e in note.entities if e.flagged]
    soap = {
        "S": f"Αναφερόμενα από τον ασθενή (απο-ταυτοποιημένα): {note.transcript_redacted.strip()}",
        "O": ("Αναγνωρισμένοι κλινικοί όροι: " +
              "; ".join(f"{e.text} → {e.snomed_display} [SNOMED {e.snomed_code}]" for e in mapped)
              if mapped else "— (δεν καταγράφηκαν αντικειμενικά ευρήματα στο απόσπασμα)"),
        "A": ("Λίστα προβλημάτων (draft): " + "; ".join(note.problem_list)
              if note.problem_list else "— (καμία κωδικοποιήσιμη εκτίμηση από το απόσπασμα)"),
        "P": ("Προς αξιολόγηση από τον ιατρό: " +
              "; ".join(e.text for e in flagged) + ". " if flagged else "") +
             "Οριστικοποίηση σχεδίου από τον θεράποντα ιατρό πριν την υπογραφή.",
        "disclaimer": note.disclaimer,
    }
    return soap


class ClinicalScribe:
    """Draft-only structuring. Drafts live in a transient buffer; only sign() writes the record."""

    def __init__(self, anonymizer: Optional[EdgeAnonymizer] = None, mapper=None) -> None:
        # clinical context → dates ARE PII here (DOB); mapper hook lets a real SNOMED server slot in.
        # use_ner=True: σε κλινικά transcripts τα ονόματα εμφανίζονται ΧΩΡΙΣ cue («ο Γιώργος ήρθε…»)
        # — το NER (Greek spaCy όταν υπάρχει) είναι απαραίτητο recall· τα cues μένουν ως offline floor.
        self.anonymizer = anonymizer or EdgeAnonymizer(use_ner=True, redact_dates=True)
        self.mapper = mapper or self._lexicon_map
        self._drafts: dict[str, ScribeNote] = {}      # transient — NOT the clinical record
        self._record: dict[str, ScribeNote] = {}      # SIGNED notes only — the record
        self._seq = 0

    # ---- draft (autonomous, but produces only a proposal) ----
    def draft_sync(self, transcript: str) -> ScribeNote:
        if not isinstance(transcript, str) or not transcript.strip():
            raise ValueError("transcript must be non-empty text")
        red = self.anonymizer.anonymize_sync(transcript)
        entities = self.mapper(red.redacted)
        problem_list = [f"{e.snomed_display} ({e.icd10_code})" for e in entities
                        if e.snomed_code and not e.flagged]
        self._seq += 1
        note = ScribeNote(
            id=f"draft-{self._seq:04d}", status=NoteStatus.DRAFT,
            transcript_redacted=red.redacted, entities=entities,
            problem_list=problem_list, pii_removed=red.pii_count,
        )
        self._drafts[note.id] = note      # buffered, NOT persisted to the record
        AUDIT.record("scribe.note_drafted", {"id": note.id, "problems": len(problem_list),
                                             "flagged": sum(1 for e in entities if e.flagged),
                                             "pii_removed": red.pii_count})
        return note

    async def draft(self, transcript: str) -> ScribeNote:
        return await asyncio.to_thread(self.draft_sync, transcript)

    # ---- sign (the only path to the clinical record) ----
    def sign(self, draft_id: str, clinician_id: str) -> ScribeNote:
        if not clinician_id or not clinician_id.strip():
            raise PermissionError("a licensed clinician_id is required to sign")
        note = self._drafts.get(draft_id)
        if note is None:
            raise KeyError(f"no draft {draft_id!r} to sign")
        signed = note.model_copy(update={"status": NoteStatus.SIGNED, "signed_by": clinician_id})
        self._record[draft_id] = signed          # NOW it enters the record
        del self._drafts[draft_id]
        AUDIT.record("scribe.note_signed", {"id": draft_id, "clinician": clinician_id})
        return signed

    def record(self) -> list[ScribeNote]:
        """The clinical record — SIGNED notes only. Drafts are never here."""
        return list(self._record.values())

    # ---- default lexicon mapper (swap via `mapper` for a real terminology server) ----
    @staticmethod
    def _lexicon_map(text: str) -> list[ClinicalEntity]:
        low = text.lower()
        entities: list[ClinicalEntity] = []
        seen: set[str] = set()
        for entry in _LEXICON:
            for term in entry["terms"]:
                if term in low and entry["snomed"][0] not in seen:
                    entities.append(ClinicalEntity(
                        text=term, snomed_code=entry["snomed"][0], snomed_display=entry["snomed"][1],
                        icd10_code=entry["icd10"], confidence=0.9, flagged=False))
                    seen.add(entry["snomed"][0])
                    break
        # flag clinician-cued phrases we could NOT map (never guess a code)
        for m in _FLAG_CUES.finditer(text):
            phrase = m.group(1).strip()
            plow = phrase.lower()
            mapped = any(term in plow for entry in _LEXICON for term in entry["terms"])
            if phrase and not mapped:
                entities.append(ClinicalEntity(text=phrase, confidence=0.3, flagged=True))
        return entities


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    scribe = ClinicalScribe()
    transcript = ("Ασθενής Γιώργος Παππάς, ΑΜΚΑ 12036512345. Παράπονο: οσφυαλγία μετά από άρση βάρους. "
                  "Αναφέρει και μυϊκό σπασμό. Σύμπτωμα: σπάνιο σύνδρομο Ζ υπό διερεύνηση.")
    note = scribe.draft_sync(transcript)
    print("DRAFT", note.id, "| PII removed:", note.pii_removed)
    for e in note.entities:
        tag = "⚠ FLAGGED" if e.flagged else f"{e.snomed_display} [{e.snomed_code}] / ICD-10 {e.icd10_code}"
        print(f"  · {e.text!r} → {tag} (conf {e.confidence})")
    print("problem list:", note.problem_list)
    print("in record before sign?:", note.id in [n.id for n in scribe.record()])
    signed = scribe.sign(note.id, clinician_id="dr_papadopoulos_AM12345")
    print("SIGNED by", signed.signed_by, "| in record now?:", signed.id in [n.id for n in scribe.record()])
