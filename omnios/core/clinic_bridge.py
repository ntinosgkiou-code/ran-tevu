"""
clinic_bridge.py — Οι «πραγματικοί υπάλληλοι» και η γέφυρα που τους συνδέει.
================================================================================
Τέσσερις domain agents (Ρεσεψιόν/Booking, Υποδοχή, Κλινικός, Λογιστήριο) που ΕΠΙΚΟΙΝΩΝΟΥΝ
μεταξύ τους μέσω ενός in-process event bus και ΕΚΤΕΛΟΥΝ πραγματικές ενέργειες πάνω στο ΚΟΙΝΟ
ClinicStore — όχι narration, πραγματικά side-effects. Η «γέφυρα» = το event bus: κάθε handoff
είναι ένα event, κάθε event καταγράφεται στην tamper-evident αλυσίδα (EU AI Act Art.12).

Ο πλήρης κύκλος ενός ασθενή τρέχει από μόνος του, χωρίς ανθρώπινη κόλλα ανάμεσα στα βήματα:

  booking.requested → [BookingAgent] add_appointment → appointment.booked
                    → [ReceptionAgent] φάκελος έτοιμος → checkin.prepared
  patient.arrived   → [ReceptionAgent] set_status → visit.started
  visit.completed   → [ClinicalAgent] SOAP draft→sign→φάκελος → note.signed + visit.billable
  visit.billable    → [BillingAgent] add_invoice + myDATA(queued) → invoice.issued
  invoice.issued    → [CheckoutAgent] follow-up + reminder(consent-gated) → checkout.done

Canonical γλώσσα = FHIR-lite payloads· ο `journey_to_fhir()` εξάγει το επεισόδιο ως FHIR R4 Bundle
(η γέφυρα προς οποιοδήποτε EHR/σύστημα μιλάει FHIR). Deterministic, stdlib. Tests: test_clinic_bridge.py
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Optional

from audit import AUDIT


class ClinicEventBus:
    """In-process pub/sub. Κάθε publish καταγράφεται στο audit (η επικοινωνία ΕΙΝΑΙ το ίχνος) και
    παραδίδεται σύγχρονα, με σειρά εγγραφής. Ένας agent που σκάει ΔΕΝ ρίχνει τον bus — το σφάλμα
    καταγράφεται και ο κύκλος συνεχίζει (isolation μεταξύ υπαλλήλων)."""

    def __init__(self) -> None:
        self._subs: dict[str, list[tuple[str, Callable]]] = defaultdict(list)
        self.trace: list[dict] = []

    def subscribe(self, event_type: str, agent: str, handler: Callable[[dict], None]) -> None:
        self._subs[event_type].append((agent, handler))

    def publish(self, event_type: str, payload: dict, source: str = "external") -> dict:
        seq = len(self.trace)
        evt = {"seq": seq, "type": event_type, "source": source, "payload": payload}
        self.trace.append(evt)
        AUDIT.record(f"bridge.{event_type}", {"seq": seq, "from": source,
                                              "ref": payload.get("ref") or payload.get("appointment_id")})
        for agent, handler in list(self._subs.get(event_type, [])):
            try:
                handler(payload)
            except Exception as e:                 # ένας υπάλληλος πέφτει → δεν πέφτει το ιατρείο
                AUDIT.record("bridge.handler_error",
                             {"event": event_type, "agent": agent, "error": f"{type(e).__name__}: {e}"})
        return evt

    def journey(self) -> list[dict]:
        return list(self.trace)


# ─────────────────────────────── OI YΠΑΛΛΗΛΟΙ ────────────────────────────────
class BookingAgent:
    """Ρεσεψιόν: δέχεται αίτημα ραντεβού → κλείνει (ή απορρίπτει διπλοκράτηση) → ειδοποιεί."""
    name = "booking"

    def __init__(self, bus: ClinicEventBus, store) -> None:
        self.bus, self.store = bus, store
        bus.subscribe("booking.requested", self.name, self.on_request)

    def on_request(self, p: dict) -> None:
        try:
            a = self.store.add_appointment(p["time"], p["patient"], p.get("service", ""),
                                           p.get("date"), p.get("value"), "confirmed", p.get("patient_id", ""))
            self.bus.publish("appointment.booked",
                             {"appointment_id": a["id"], "patient": a["patient"],
                              "patient_id": p.get("patient_id", ""), "date": a["date"],
                              "time": a["time"], "value": a["value"], "ref": a["id"]}, source=self.name)
        except ValueError as e:                    # slot πιασμένο → πρότεινε waitlist, μη σκας
            self.store.add_waitlist(p["patient"], f"ήθελε {p.get('date','')} {p['time']}")
            self.bus.publish("appointment.rejected",
                             {"patient": p["patient"], "reason": str(e), "ref": None}, source=self.name)


class ReceptionAgent:
    """Υποδοχή: ετοιμάζει τον φάκελο στην κράτηση· ξεκινά την επίσκεψη όταν φτάνει ο ασθενής."""
    name = "reception"

    def __init__(self, bus: ClinicEventBus, store) -> None:
        self.bus, self.store = bus, store
        bus.subscribe("appointment.booked", self.name, self.on_booked)
        bus.subscribe("patient.arrived", self.name, self.on_arrived)

    def on_booked(self, p: dict) -> None:
        # εξασφάλισε φάκελο ασθενή (χωρίς διπλότυπο) — check-in έτοιμο πριν καν φτάσει
        exists = any(x["name"] == p["patient"] for x in self.store.list_patients())
        if not exists:
            self.store.add_patient(p["patient"], consent=True)
        self.bus.publish("checkin.prepared",
                         {"appointment_id": p["appointment_id"], "patient": p["patient"],
                          "patient_id": p.get("patient_id", ""), "ref": p["appointment_id"]}, source=self.name)

    def on_arrived(self, p: dict) -> None:
        try:
            self.store.set_status(p["appointment_id"], "arrived")
        except KeyError:
            return
        self.bus.publish("visit.started",
                         {"appointment_id": p["appointment_id"], "patient": p["patient"],
                          "patient_id": p.get("patient_id", ""), "ref": p["appointment_id"]}, source=self.name)


class ClinicalAgent:
    """Κλινικός: στο τέλος της επίσκεψης δομεί SOAP από το transcript, το υπογράφει (αδειούχος),
    το βάζει στον φάκελο, και σηματοδοτεί ότι η επίσκεψη είναι χρεώσιμη."""
    name = "clinical"

    def __init__(self, bus: ClinicEventBus, store, scribe) -> None:
        self.bus, self.store, self.scribe = bus, store, scribe
        bus.subscribe("visit.completed", self.name, self.on_completed)

    def on_completed(self, p: dict) -> None:
        transcript = p.get("transcript", "")
        clinician = p.get("clinician_id", "dr-unknown")
        note_id = None
        if transcript.strip():
            draft = self.scribe.draft_sync(transcript)
            signed = self.scribe.sign(draft.id, clinician)                 # μόνο αδειούχος υπογράφει
            # body = η δομημένη λίστα προβλημάτων (SNOMED/ICD) — το πραγματικό κλινικό περιεχόμενο·
            # fallback = ΑΠΟ-ΤΑΥΤΟΠΟΙΗΜΕΝΟ transcript (transcript_redacted), ΠΟΤΕ raw PII.
            body = "; ".join(signed.problem_list) if signed.problem_list else signed.transcript_redacted[:400]
            rec = self.store.add_note({"patient": p["patient"], "kind": "soap",
                                       "body": body, "signed_by": clinician})
            note_id = rec["id"]
            self.bus.publish("note.signed",
                             {"appointment_id": p["appointment_id"], "note_id": note_id,
                              "patient": p["patient"], "ref": note_id}, source=self.name)
        try:
            self.store.set_status(p["appointment_id"], "completed")
        except KeyError:
            pass
        self.bus.publish("visit.billable",
                         {"appointment_id": p["appointment_id"], "patient": p["patient"],
                          "patient_id": p.get("patient_id", ""), "value": p.get("value"),
                          "note_id": note_id, "ref": p["appointment_id"]}, source=self.name)


class BillingAgent:
    """Λογιστήριο: εκδίδει τιμολόγιο για τη χρεώσιμη επίσκεψη και το βάζει σε ουρά myDATA
    (fail-honest — χωρίς AADE credentials μένει queued με τον λόγο, ποτέ ψεύτικη υποβολή)."""
    name = "billing"

    def __init__(self, bus: ClinicEventBus, store, clinic_afm: str = "") -> None:
        self.bus, self.store, self.afm = bus, store, clinic_afm
        bus.subscribe("visit.billable", self.name, self.on_billable)

    def on_billable(self, p: dict) -> None:
        val = p.get("value")
        if val is None:
            val = self.store.get_clinic().default_value
        # IDEMPOTENT: το store κάνει auto-invoice όταν ένα ραντεβού γίνει 'completed'
        # (set_status). Ο BillingAgent ΔΕΝ διπλο-χρεώνει — αν υπάρχει ήδη τιμολόγιο για αυτό το
        # ραντεβού (από το auto-invoice ή προηγούμενο event), το ΕΠΑΝΑΧΡΗΣΙΜΟΠΟΙΕΙ. Έτσι ο κλινικός
        # και ο λογιστής «συνεννοούνται» σωστά αντί να παράγουν δύο τιμολόγια για μία επίσκεψη.
        aid = p["appointment_id"]
        inv = next((i for i in self.store.list_invoices() if i.get("appointment_id") == aid), None)
        if inv is None:
            inv = self.store.add_invoice(p["patient"], float(val), "unpaid",
                                         p.get("patient_id", ""), aid)
        mydata_status = "not_attempted"
        if self.afm:
            from mydata_gr import submit_invoice
            r = submit_invoice(self.store, inv["id"], {"afm": self.afm, "branch": 0}, sandbox=True)
            mydata_status = r.get("status", "queued")
        self.bus.publish("invoice.issued",
                         {"appointment_id": p["appointment_id"], "invoice_id": inv["id"],
                          "patient": p["patient"], "patient_id": p.get("patient_id", ""),
                          "amount": inv["amount"], "mydata": mydata_status, "ref": inv["id"]}, source=self.name)


class CheckoutAgent:
    """Αναχώρηση: κλείνει follow-up και στέλνει υπενθύμιση — ΜΟΝΟ αν υπάρχει συναίνεση (GDPR Art.9)·
    χωρίς provider keys η υπενθύμιση μπαίνει στην durable ουρά (τίποτα δεν χάνεται/ψεύτικο)."""
    name = "checkout"

    def __init__(self, bus: ClinicEventBus, store, channels, followup_days: int = 30) -> None:
        self.bus, self.store, self.channels, self.days = bus, store, channels, followup_days
        bus.subscribe("invoice.issued", self.name, self.on_issued)

    def on_issued(self, p: dict) -> None:
        from datetime import datetime, timedelta
        # 1) follow-up ραντεβού (χωρίς πραγματικό clock εξάρτηση — σχετική ημερομηνία)
        fdate = (datetime.now() + timedelta(days=self.days)).strftime("%Y-%m-%d")
        followup_id = None
        try:
            f = self.store.add_appointment("09:00", p["patient"], "Επανεξέταση", fdate,
                                           None, "confirmed", p.get("patient_id", ""))
            followup_id = f["id"]
        except ValueError:
            pass                                    # 09:00 πιασμένο εκείνη τη μέρα — αγνόησε ήσυχα
        # 2) υπενθύμιση — consent gate ΠΑΝΤΑ πριν από κάθε επικοινωνία με ασθενή
        contact = self._contact_for(p["patient"], p.get("patient_id", ""))
        reminder_status = "no_consent"
        if self.store.consent_for(p["patient"], p.get("patient_id", "")) and contact:
            r = self.channels.dispatch("sms", contact,
                                       f"Υπενθύμιση: επανεξέταση {fdate} 09:00.",
                                       why="checkout_followup", approved_by=self.name)
            reminder_status = r["status"]
        self.bus.publish("checkout.done",
                         {"appointment_id": p["appointment_id"], "followup_id": followup_id,
                          "reminder": reminder_status, "patient": p["patient"],
                          "ref": followup_id}, source=self.name)

    def _contact_for(self, name: str, patient_id: str) -> str:
        for x in self.store.list_patients():
            if (patient_id and x["id"] == patient_id) or (not patient_id and x["name"] == name):
                return x.get("phone", "")
        return ""


# ─────────────────────────── Το ενοποιημένο ιατρείο ──────────────────────────
class ClinicBridge:
    """Στήνει το event bus + τους 5 υπαλλήλους πάνω σε ΕΝΑ ClinicStore. Ένα `bridge` = ένα ιατρείο
    όπου οι υπάλληλοι συνεννοούνται μόνοι τους. Helpers για να τρέξεις έναν πλήρη κύκλο ασθενή."""

    def __init__(self, store, channels=None, scribe=None, clinic_afm: str = "", bus=None) -> None:
        self.store = store
        # bus=None → δικό του· δώσε κοινό bus ώστε reactive (journey) + proactive (autonomous)
        # agents να μοιράζονται ΕΝΑ nervous system (μία ενοποιημένη ροή + audit).
        self.bus = bus if bus is not None else ClinicEventBus()
        if channels is None:
            from channels import MessageChannels
            channels = MessageChannels(store)
        if scribe is None:
            from clinical_scribe import ClinicalScribe
            scribe = ClinicalScribe()
        self.channels = channels
        self.agents = {
            "booking": BookingAgent(self.bus, store),
            "reception": ReceptionAgent(self.bus, store),
            "clinical": ClinicalAgent(self.bus, store, scribe),
            "billing": BillingAgent(self.bus, store, clinic_afm),
            "checkout": CheckoutAgent(self.bus, store, channels),
        }

    def request_booking(self, patient: str, time: str, date: str = None,
                        service: str = "Συνεδρία", value: float = None, patient_id: str = "") -> dict:
        return self.bus.publish("booking.requested",
                                {"patient": patient, "time": time, "date": date,
                                 "service": service, "value": value, "patient_id": patient_id},
                                source="external")

    def arrive(self, appointment_id: str, patient: str, patient_id: str = "") -> dict:
        return self.bus.publish("patient.arrived",
                                {"appointment_id": appointment_id, "patient": patient,
                                 "patient_id": patient_id}, source="external")

    def complete_visit(self, appointment_id: str, patient: str, transcript: str,
                       clinician_id: str, value: float = None, patient_id: str = "") -> dict:
        return self.bus.publish("visit.completed",
                                {"appointment_id": appointment_id, "patient": patient,
                                 "transcript": transcript, "clinician_id": clinician_id,
                                 "value": value, "patient_id": patient_id}, source="external")

    def journey(self) -> list[dict]:
        return self.bus.journey()


def journey_to_fhir(bridge: ClinicBridge, patient_token: str = "patient-tokenized") -> dict:
    """Εξάγει το τελευταίο επεισόδιο (Encounter + Invoice→Claim) ως FHIR R4 Bundle — η «γέφυρα»
    προς οποιοδήποτε EHR/payer μιλάει FHIR. Μηδέν PII: μόνο vault token, ποτέ όνομα/ΑΜΚΑ."""
    tr = bridge.journey()
    booked = next((e for e in tr if e["type"] == "appointment.booked"), None)
    issued = next((e for e in tr if e["type"] == "invoice.issued"), None)
    cid = (issued or booked or {"payload": {"ref": "episode"}})["payload"].get("ref") or "episode"
    # Δεν επινοούμε US CPT/ICD codes για ελληνική επίσκεψη (θα ήταν ανέντιμο) — το Bundle δείχνει
    # τη ΔΟΜΗ της γέφυρας (Patient+Encounter+Claim). Το ποσό μπαίνει ως custom extension, όχι fake code.
    claim = {"claim_id": str(cid), "encounter": {"total_minutes": None},
             "diagnoses": [], "procedures": []}
    from fhir_adapter import claim_to_fhir
    bundle = claim_to_fhir(claim, patient_token=patient_token)
    if issued:
        for entry in bundle["entry"]:
            if entry["resource"].get("resourceType") == "Claim":
                entry["resource"].setdefault("extension", []).append(
                    {"url": "urn:omnios:billed-amount-eur",
                     "valueDecimal": float(issued["payload"].get("amount", 0))})
    return bundle
