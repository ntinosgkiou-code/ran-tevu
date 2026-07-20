"""
clinic_bridge.py — Ran-Tevu: οι «πραγματικοί υπάλληλοι» και η γέφυρα που τους συνδέει.
================================================================================
Ίδιο pattern με το OmniOS clinic_bridge, προσαρμοσμένο στις ΠΡΑΓΜΑΤΙΚΕΣ συναρτήσεις του Ran-Tevu
(database.py + back_office.py) και στο multi-tenant μοντέλο (per-slug). Πέντε domain agents που
ΕΠΙΚΟΙΝΩΝΟΥΝ μέσω event bus και ΕΚΤΕΛΟΥΝ πραγματικά side-effects — όχι narration.

  booking.requested → [BookingAgent] create_booking → appointment.booked
                    → [ReceptionAgent] φάκελος (upsert_patient) → checkin.prepared
  patient.arrived   → [ReceptionAgent] → visit.started
  visit.completed   → [ClinicalAgent] σημείωση (encrypted) + resolve_booking('completed')
                       → note.signed + visit.billable
  visit.billable    → [BillingAgent] (auto-invoice ήδη έγινε· idempotent) → invoice.issued
  invoice.issued    → [CheckoutAgent] follow-up + υπενθύμιση (consent-gated) → checkout.done

Κάθε handoff → database.log_audit (η επικοινωνία ΕΙΝΑΙ το ίχνος). Ένας agent που σκάει δεν ρίχνει
τον bus. Tests: python tests/test_clinic_bridge.py
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Callable, Optional

import database as db
import back_office as bo


class ClinicEventBus:
    """In-process pub/sub, per-slug. Κάθε publish καταγράφεται στο audit trail του Ran-Tevu."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self._subs: dict[str, list[tuple[str, Callable]]] = defaultdict(list)
        self.trace: list[dict] = []

    def subscribe(self, event_type: str, agent: str, handler: Callable[[dict], None]) -> None:
        self._subs[event_type].append((agent, handler))

    def publish(self, event_type: str, payload: dict, source: str = "external") -> dict:
        seq = len(self.trace)
        evt = {"seq": seq, "type": event_type, "source": source, "payload": payload}
        self.trace.append(evt)
        try:
            db.log_audit(action=f"bridge.{event_type}", slug=self.slug,
                         actor_type="agent", actor_id=source,
                         details=str(payload.get("ref") or payload.get("booking_id") or ""))
        except Exception:
            pass
        for agent, handler in list(self._subs.get(event_type, [])):
            try:
                handler(payload)
            except Exception as e:
                try:
                    db.log_audit(action="bridge.handler_error", slug=self.slug,
                                 actor_type="agent", actor_id=agent,
                                 details=f"{event_type}: {type(e).__name__}: {e}")
                except Exception:
                    pass
        return evt

    def journey(self) -> list[dict]:
        return list(self.trace)


# ─────────────────────────────── ΟΙ ΥΠΑΛΛΗΛΟΙ ────────────────────────────────
class BookingAgent:
    name = "booking"

    def __init__(self, bus: ClinicEventBus, slug: str) -> None:
        self.bus, self.slug = bus, slug
        bus.subscribe("booking.requested", self.name, self.on_request)

    def on_request(self, p: dict) -> None:
        try:
            bid = db.create_booking(self.slug, p["date"], p["time"], p.get("service", "Συνεδρία"),
                                    int(p.get("duration", 30)), p["patient"], p["phone"],
                                    p.get("email", ""), p.get("notes", ""))
            self.bus.publish("appointment.booked",
                             {"booking_id": bid, "patient": p["patient"], "phone": p["phone"],
                              "email": p.get("email", ""), "date": p["date"], "time": p["time"],
                              "value": p.get("value"), "ref": bid}, source=self.name)
        except ValueError as e:                    # slot πιασμένο → waitlist, μη σκας
            db.add_to_waitlist(self.slug, p["date"], p["time"], p.get("service", "Συνεδρία"),
                               p["patient"], p["phone"], p.get("email", ""))
            self.bus.publish("appointment.rejected",
                             {"patient": p["patient"], "reason": str(e), "ref": None}, source=self.name)


class ReceptionAgent:
    name = "reception"

    def __init__(self, bus: ClinicEventBus, slug: str) -> None:
        self.bus, self.slug = bus, slug
        bus.subscribe("appointment.booked", self.name, self.on_booked)
        bus.subscribe("patient.arrived", self.name, self.on_arrived)

    def on_booked(self, p: dict) -> None:
        bo.upsert_patient(self.slug, p["phone"], p["patient"], p.get("email", ""))   # φάκελος έτοιμος
        bo.link(self.slug, "patient", p["phone"], "booking", p["booking_id"], "appointment")
        self.bus.publish("checkin.prepared",
                         {"booking_id": p["booking_id"], "patient": p["patient"],
                          "phone": p["phone"], "ref": p["booking_id"]}, source=self.name)

    def on_arrived(self, p: dict) -> None:
        self.bus.publish("visit.started",
                         {"booking_id": p["booking_id"], "patient": p["patient"],
                          "phone": p["phone"], "ref": p["booking_id"]}, source=self.name)


class ClinicalAgent:
    name = "clinical"

    def __init__(self, bus: ClinicEventBus, slug: str) -> None:
        self.bus, self.slug = bus, slug
        bus.subscribe("visit.completed", self.name, self.on_completed)

    def on_completed(self, p: dict) -> None:
        note = (p.get("notes") or "").strip()
        note_ok = False
        if note:
            bo.add_patient_note(self.slug, p["phone"], note, kind="clinical",
                                title="Σημείωση επίσκεψης", author="γιατρός",
                                booking_id=p.get("booking_id"))
            note_ok = True
            self.bus.publish("note.signed",
                             {"booking_id": p["booking_id"], "patient": p["patient"],
                              "ref": p["booking_id"]}, source=self.name)
        # ολοκλήρωση επίσκεψης → auto-invoice (idempotent στο back_office)
        r = bo.resolve_booking(self.slug, p["booking_id"], "completed")
        self.bus.publish("visit.billable",
                         {"booking_id": p["booking_id"], "patient": p["patient"], "phone": p["phone"],
                          "value": p.get("value"), "resolved": bool(r and r.get("ok")),
                          "note": note_ok, "ref": p["booking_id"]}, source=self.name)


class BillingAgent:
    name = "billing"

    def __init__(self, bus: ClinicEventBus, slug: str) -> None:
        self.bus, self.slug = bus, slug
        bus.subscribe("visit.billable", self.name, self.on_billable)

    def on_billable(self, p: dict) -> None:
        # IDEMPOTENT: το resolve_booking('completed') → autoinvoice_completed_bookings ήδη εξέδωσε
        # τιμολόγιο για αυτό το ραντεβού. Ο BillingAgent ΔΕΝ διπλο-χρεώνει — βρίσκει το υπάρχον·
        # μόνο αν ΔΕΝ υπάρχει (π.χ. δεν πέρασε από resolve) εκδίδει ο ίδιος. Έτσι κλινικός & λογιστής
        # «συνεννοούνται» αντί να βγάζουν δύο τιμολόγια για μία επίσκεψη.
        with db._conn() as c:
            inv = c.execute("SELECT id, total FROM bo_invoices WHERE slug=? AND booking_id=? LIMIT 1",
                            (self.slug, p["booking_id"])).fetchone()
        if inv:
            iid, amount = inv["id"], inv["total"]
        else:
            from vertical_templates import vat_rate_for
            biz = db.get_business(self.slug)
            vat = vat_rate_for(biz["type"] if biz else "")
            val = p.get("value")
            if val is None:
                val = 40.0
            iid = bo.create_invoice(self.slug, p["phone"], p["patient"], "Συνεδρία", float(val),
                                    vat_rate=vat, booking_id=p["booking_id"], status="unpaid")
            amount = float(val)
        self.bus.publish("invoice.issued",
                         {"booking_id": p["booking_id"], "invoice_id": iid, "patient": p["patient"],
                          "phone": p["phone"], "amount": amount, "ref": iid}, source=self.name)


class CheckoutAgent:
    name = "checkout"

    def __init__(self, bus: ClinicEventBus, slug: str, followup_days: int = 30) -> None:
        self.bus, self.slug, self.days = bus, slug, followup_days
        bus.subscribe("invoice.issued", self.name, self.on_issued)

    def on_issued(self, p: dict) -> None:
        fdate = (datetime.now() + timedelta(days=self.days)).strftime("%Y-%m-%d")
        followup_id = None
        try:
            followup_id = db.create_booking(self.slug, fdate, "09:00", "Επανεξέταση", 30,
                                            p["patient"], p["phone"], "", "[follow-up]")
        except ValueError:
            pass                                   # 09:00 πιασμένο εκείνη τη μέρα — αγνόησε ήσυχα
        # υπενθύμιση — συντηρητικό consent gate (ασφαλές): στέλνει μόνο αν υπάρχει καταγεγραμμένη
        # συναίνεση επικοινωνίας· αλλιώς μόνο κλείνει το follow-up (τίποτα δεν φεύγει).
        reminder = "no_consent"
        cs = db.get_consent_status(p["phone"], consent_type="reminders")
        if cs and cs.get("accepted"):
            sent = bo._send_patient_message(self.slug, p["phone"], p["patient"],
                                            f"Υπενθύμιση: επανεξέταση {fdate} 09:00.",
                                            subject="Υπενθύμιση επανεξέτασης")
            reminder = "sent" if sent else "queued"
        self.bus.publish("checkout.done",
                         {"booking_id": p["booking_id"], "followup_id": followup_id,
                          "reminder": reminder, "patient": p["patient"], "ref": followup_id},
                         source=self.name)


# ─────────────────────────── Το ενοποιημένο ιατρείο ──────────────────────────
class ClinicBridge:
    """Στήνει event bus + 5 υπαλλήλους για ΕΝΑ ιατρείο (slug). Οι υπάλληλοι συνεννοούνται μόνοι τους."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.bus = ClinicEventBus(slug)
        self.agents = {
            "booking": BookingAgent(self.bus, slug),
            "reception": ReceptionAgent(self.bus, slug),
            "clinical": ClinicalAgent(self.bus, slug),
            "billing": BillingAgent(self.bus, slug),
            "checkout": CheckoutAgent(self.bus, slug),
        }

    def request_booking(self, patient, phone, time, date, service="Συνεδρία",
                        value=None, email="") -> dict:
        return self.bus.publish("booking.requested",
                                {"patient": patient, "phone": phone, "time": time, "date": date,
                                 "service": service, "value": value, "email": email}, source="external")

    def arrive(self, booking_id, patient, phone) -> dict:
        return self.bus.publish("patient.arrived",
                                {"booking_id": booking_id, "patient": patient, "phone": phone},
                                source="external")

    def complete_visit(self, booking_id, patient, phone, notes="", value=None) -> dict:
        return self.bus.publish("visit.completed",
                                {"booking_id": booking_id, "patient": patient, "phone": phone,
                                 "notes": notes, "value": value}, source="external")

    def journey(self) -> list[dict]:
        return self.bus.journey()
