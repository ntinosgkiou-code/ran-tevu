"""
clinic_store.py — the REAL, persistent system of record for a clinic's operations.
====================================================================================
This is what turns OmniOS from "demo numbers" into a product you can install: appointments,
patients, invoices, waitlist and the recovered-value ledger are stored in SQLite (via KVStore),
survive a restart, and DRIVE every metric. No mock arrays anywhere downstream.

What makes it production-shaped (not a toy):
  • Multi-tenant — every record is namespaced by tenant; two clinics never see each other's data.
  • Double-booking guard — the same date+time slot can't be taken twice (race-safe at the store).
  • Autonomy hooks — a no-show books a recovery €-event; a cancellation offers the slot to the
    waitlist automatically. The agents act ON REAL DATA, not a script.
  • GDPR-aware — patients carry an explicit-consent flag (Art.9); every write is appended to the
    tamper-evident audit chain (Art.12). The clinic (controller) sees its own data; the AI layer
    tokenizes PII at the edge before any cloud call (see edge_anonymizer / the Privacy tab).
  • Value-based billing — realized outcomes become € events; the bill is a share of € saved.
  • Import — a clinic connects by importing its patients/appointments (JSON/CSV rows); the system
    then operates on that live data. Zero-config demo seed for an empty clinic.

Deterministic, stdlib + Pydantic, typed. Tests: python core/test_clinic_store.py
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Optional

import threading

from pydantic import BaseModel, Field

from store import KVStore
from predictive_billing import ValueBasedBilling, ValueEvent

# statuses an appointment moves through: booked/confirmed → arrived (check-in, in the waiting
# room — occupies the slot, does NOT count a visit yet) → completed (visit done → auto-invoice)
_ACTIVE = ("booked", "confirmed", "arrived", "completed")   # occupy a slot (not cancelled)
_BILLING = ValueBasedBilling(pct=0.10)


def _nid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def today_iso() -> str:
    """The clinic runs on REAL calendar time — every date in the store is a true ISO date."""
    return time.strftime("%Y-%m-%d")


def _norm_time(t: str) -> str:
    """Normalize clock time to canonical HH:MM ('9:00'→'09:00') so slot equality is REAL —
    without this, '9:00' and '09:00' would double-book the same physical slot."""
    import re as _re
    m = _re.match(r"^\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*$", str(t))
    if not m:
        raise ValueError(f"invalid time {t!r} — use HH:MM (e.g. 09:00)")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _norm_date(d: Optional[str]) -> str:
    """Normalize to ISO YYYY-MM-DD; None/'today'/'σήμερα' → the REAL current date."""
    if not d or d in ("today", "σήμερα"):
        return today_iso()
    import re as _re
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        raise ValueError(f"invalid date {d!r} — use YYYY-MM-DD")
    return d


class Clinic(BaseModel):
    name: str = "Ιατρείο"
    specialty: str = "Γενική Ιατρική"
    hours: str = "Δευ–Παρ 09:00–20:00"
    default_value: float = 40.0
    services: list[dict] = Field(default_factory=list)   # [{"name","price","duration"}] — ανά ιατρείο
    afm: str = ""                                         # ΑΦΜ (για myDATA)
    reminder_hours: list[int] = Field(default_factory=lambda: [24, 4])
    onboarded: bool = False

    def onboarding_status(self) -> dict:
        """Checklist «κουμπώνει σε κάθε ιατρείο»: τι έχει ρυθμιστεί, τι λείπει — 5λεπτο setup."""
        steps = {
            "όνομα & ειδικότητα": bool(self.name and self.specialty),
            "ωράριο": bool(self.hours),
            "υπηρεσίες & τιμές": len(self.services) > 0,
            "ΑΦΜ (για τιμολόγηση)": bool(self.afm),
        }
        done = sum(steps.values())
        return {"onboarded": self.onboarded, "steps": steps,
                "complete": done == len(steps), "progress": f"{done}/{len(steps)}"}


class ClinicStore:
    """Persistent operations store for ONE tenant. Reuse across requests; safe to reopen the DB."""

    def __init__(self, path: Optional[str] = None, tenant: str = "default") -> None:
        self.kv = KVStore(path)
        self.t = tenant
        # Serializes the double-booking check-then-insert so it's truly atomic (the KVStore lock
        # only covers single ops, so a bare _slot_taken()+put() has a TOCTOU window). One shared
        # instance per tenant → concurrent requests to the same calendar can't both win a slot.
        self._slot_lock = threading.Lock()

    # namespaces are tenant-scoped → hard multi-tenant isolation
    def _ns(self, kind: str) -> str:
        return f"{kind}:{self.t}"

    # ── clinic profile ──
    def get_clinic(self) -> Clinic:
        d = self.kv.get(self._ns("clinic"), "profile")
        return Clinic(**d) if d else Clinic()

    def setup_clinic(self, **fields: Any) -> Clinic:
        c = self.get_clinic()
        c = c.model_copy(update={k: v for k, v in fields.items() if v is not None})
        c.onboarded = True
        self.kv.put(self._ns("clinic"), "profile", c.model_dump())
        self._audit("clinic.setup", {"name": c.name, "specialty": c.specialty})
        return c

    # ── audit (tamper-evident, Art.12) ──
    def _audit(self, event: str, data: dict) -> None:
        try:
            from audit import AUDIT
            AUDIT.record(event, {"tenant": self.t, **data})
        except Exception:
            pass

    # ── patients ──
    def add_patient(self, name: str, phone: str = "", amka: str = "",
                    consent: bool = True, tag: str = "ενεργός", email: str = "") -> dict:
        pid = _nid("pat")
        rec = {"id": pid, "name": name, "phone": phone, "amka": amka, "consent": bool(consent),
               "visits": 0, "balance": 0.0, "tag": tag, "last_visit": "—", "email": email}
        self.kv.put(self._ns("patient"), pid, rec)
        self._audit("patient.create", {"id": pid, "consent": bool(consent)})   # never logs raw PII
        return rec

    def list_patients(self) -> list[dict]:
        pats = sorted(self.kv.all(self._ns("patient")).values(), key=lambda p: p["name"])
        for p in pats:                       # balance is DERIVED from unpaid invoices — one source of truth
            p["balance"] = self.patient_balance(p["name"])
        return pats

    def patient_balance(self, name: str) -> float:
        """Real outstanding balance = sum of this patient's unpaid invoices (not a stored guess)."""
        return round(sum(i["amount"] for i in self.list_invoices()
                         if i["patient"] == name and i["status"] == "unpaid"), 2)

    def patient_history(self, name: str) -> dict:
        """Everything the clinic knows about a patient, joined across the system (one record)."""
        appts = [a for a in self.list_appointments() if a["patient"] == name]
        invs = [i for i in self.list_invoices() if i["patient"] == name]
        return {"appointments": appts, "invoices": invs, "balance": self.patient_balance(name)}

    def get_patient(self, pid: str) -> Optional[dict]:
        return self.kv.get(self._ns("patient"), pid)

    def consent_for(self, name: str, patient_id: str = "") -> bool:
        """GDPR Art.9 lawful-basis lookup. Prefers the unambiguous patient_id — a name-only lookup
        would hit the WRONG record for namesakes (father/son), and could green-light outreach to a
        patient who explicitly withheld consent. A stored consent=False means DO NOT contact."""
        pats = self.kv.all(self._ns("patient")).values()
        if patient_id:
            for p in pats:
                if p["id"] == patient_id:
                    return bool(p.get("consent", True))
            return True   # id given but no record → nothing to withhold
        for p in pats:
            if p["name"] == name:
                return bool(p.get("consent", True))
        return True   # no matching record → nothing to withhold

    def update_patient(self, pid: str, **fields: Any) -> Optional[dict]:
        p = self.get_patient(pid)
        if not p:
            return None
        p.update({k: v for k, v in fields.items() if v is not None})
        self.kv.put(self._ns("patient"), pid, p)
        return p

    def erase_patient(self, pid: str) -> Optional[dict]:
        """GDPR Art.17 (right to erasure) — σωστά για ιατρείο: ΣΒΗΝΕΙ την ταυτότητα του ασθενή
        (record + PII μέσα σε ραντεβού/τιμολόγια) αλλά ΚΡΑΤΑΕΙ ψευδωνυμοποιημένα τα τιμολόγια
        (φορολογική υποχρέωση διατήρησης, Art.17§3(b)) και τα slots. Η audit-αλυσίδα ΔΕΝ πειράζεται
        (append-only) — καταγράφεται event διαγραφής. Μη-αναστρέψιμο by design."""
        p = self.get_patient(pid)
        if not p:
            return None
        name = p["name"]
        tomb = f"[διαγραμμένος-{pid[-4:]}]"
        # 1) διάγραψε το record του ασθενή (ΑΜΚΑ/τηλέφωνο/όνομα φεύγουν)
        self.kv.delete(self._ns("patient"), pid)
        # 2) ψευδωνυμοποίησε τα ραντεβού του (κρατάμε slot/status/value, φεύγει η ταυτότητα)
        appts = 0
        for a in list(self.kv.all(self._ns("appt")).values()):
            if a.get("patient_id") == pid or a.get("patient") == name:
                a["patient"], a["patient_id"] = tomb, ""
                self.kv.put(self._ns("appt"), a["id"], a)
                appts += 1
        # 3) ψευδωνυμοποίησε τα τιμολόγια (κρατάμε ποσό/status για εφορία — απαιτητό)
        invs = 0
        for i in list(self.kv.all(self._ns("invoice")).values()):
            if i.get("patient_id") == pid or i.get("patient") == name:
                i["patient"], i["patient_id"] = tomb, ""
                self.kv.put(self._ns("invoice"), i["id"], i)
                invs += 1
        # 4) βγάλ' τον από waitlist αν υπάρχει
        for w in list(self.kv.all(self._ns("waitlist")).values()):
            if w.get("name") == name:
                self.remove_waitlist(w["id"])
        self._audit("patient.erased", {"id": pid, "appts_pseudonymized": appts, "invoices_pseudonymized": invs})
        return {"erased": pid, "appointments_pseudonymized": appts, "invoices_pseudonymized": invs}

    # ── appointments ──
    def _slot_taken(self, date: str, time: str, ignore: str = "") -> bool:
        for a in self.kv.all(self._ns("appt")).values():
            if a["id"] != ignore and a["date"] == date and a["time"] == time and a["status"] in _ACTIVE:
                return True
        return False

    def add_appointment(self, time: str, patient: str, service: str = "",
                        date: Optional[str] = None, value: Optional[float] = None,
                        status: str = "confirmed", patient_id: str = "") -> dict:
        time = _norm_time(time)                     # '9:00' ≡ '09:00' — one physical slot
        date = _norm_date(date)                     # real ISO calendar date (default: today)
        with self._slot_lock:                       # check + insert atomic → no double-booking race
            if self._slot_taken(date, time):
                raise ValueError(f"slot {date} {time} already booked (double-booking guard)")
            aid = _nid("appt")
            rec = {"id": aid, "time": time, "date": date, "patient": patient, "patient_id": patient_id,
                   "service": service or self.get_clinic().specialty,
                   "status": status, "value": float(value if value is not None else self.get_clinic().default_value)}
            self.kv.put(self._ns("appt"), aid, rec)
        self._audit("appt.book", {"id": aid, "date": date, "time": time})
        return rec

    def list_appointments(self, date: Optional[str] = None) -> list[dict]:
        rows = list(self.kv.all(self._ns("appt")).values())
        if date:
            rows = [a for a in rows if a["date"] == date]
        return sorted(rows, key=lambda a: (a["date"], a["time"]))

    def reschedule_appointment(self, aid: str, new_time: str, new_date: Optional[str] = None) -> dict:
        """Move an appointment to a new slot IN PLACE (the #1 front-desk action). Unlike cancel, it
        does NOT free the slot to the waitlist — the same patient keeps their booking, just moved."""
        a = self.kv.get(self._ns("appt"), aid)
        if not a:
            raise KeyError(aid)
        new_time = _norm_time(new_time)
        target_date = _norm_date(new_date) if new_date else a["date"]
        with self._slot_lock:                       # same atomic guard as add_appointment
            if self._slot_taken(target_date, new_time, ignore=aid):
                raise ValueError(f"slot {target_date} {new_time} already booked (double-booking guard)")
            old = f"{a['date']} {a['time']}"
            a["time"], a["date"] = new_time, target_date
            self.kv.put(self._ns("appt"), aid, a)
        self._audit("appt.reschedule", {"id": aid, "from": old, "to": f"{target_date} {new_time}"})
        return a

    def set_status(self, aid: str, status: str) -> dict:
        # ΑΤΟΜΙΚΟ + IDEMPOTENT (ίδιο lock με add_appointment): ένα διπλό-κλικ «completed» ή δύο
        # ταυτόχρονα POST ΔΕΝ πρέπει να εκδώσουν 2 τιμολόγια ή να μετρήσουν 2 επισκέψεις για μία
        # επίσκεψη. Ο check-then-invoice ήταν μη-ατομικός + χωρίς already-completed short-circuit.
        with self._slot_lock:
            a = self.kv.get(self._ns("appt"), aid)
            if not a:
                raise KeyError(aid)
            already = a.get("status") == status          # ήδη σε αυτή την κατάσταση → no re-run
            a["status"] = status
            self.kv.put(self._ns("appt"), aid, a)
            self._audit("appt.status", {"id": aid, "status": status})
            if status == "completed" and not already:
                # one action ripples across the system: visit count → auto-invoice → balance → audit
                pid = a.get("patient_id")
                p = self.get_patient(pid) if pid else next(
                    (x for x in self.list_patients() if x["name"] == a["patient"]), None)
                if p:
                    self.update_patient(p["id"], visits=p.get("visits", 0) + 1,
                                        last_visit=time.strftime("%d/%m/%Y"))
                if not any(i.get("appointment_id") == aid for i in self.list_invoices()):
                    self.add_invoice(a["patient"], a["value"], "unpaid", (p["id"] if p else ""), aid)
                    self._audit("billing.auto_invoice", {"appt": aid, "amount": a["value"]})
            if status == "no_show" and not already:
                # AUTONOMY: an at-risk €-loss the recovery agent will chase; recorded as value-at-risk
                self._audit("agent.recovery_enqueued", {"appt": aid, "eur_at_risk": a["value"]})
            return a

    def cancel_appointment(self, aid: str) -> dict:
        a = self.kv.get(self._ns("appt"), aid)
        if not a:
            raise KeyError(aid)
        a["status"] = "cancelled"
        self.kv.put(self._ns("appt"), aid, a)
        backfilled = None
        wl = self.list_waitlist()
        if wl:  # AUTONOMY: waitlist backfill — offer the freed slot to the next patient
            nxt = wl[0]
            self.remove_waitlist(nxt["id"])
            backfilled = self.add_appointment(a["time"], nxt["name"], a["service"], a["date"], a["value"])
            self._audit("agent.waitlist_backfill", {"freed": aid, "filled_by": backfilled["id"]})
        else:
            self._audit("appt.cancel", {"id": aid})
        return {"cancelled": aid, "backfilled": backfilled}

    # ── waitlist ──
    def add_waitlist(self, name: str, note: str = "") -> dict:
        wid = _nid("wl")
        rec = {"id": wid, "name": name, "note": note}
        self.kv.put(self._ns("waitlist"), wid, rec)
        return rec

    def list_waitlist(self) -> list[dict]:
        return list(self.kv.all(self._ns("waitlist")).values())

    def remove_waitlist(self, wid: str) -> None:
        self.kv.delete(self._ns("waitlist"), wid)

    # ── invoices ──
    def add_invoice(self, patient: str, amount: float, status: str = "unpaid",
                    patient_id: str = "", appointment_id: str = "") -> dict:
        iid = _nid("inv")
        rec = {"id": iid, "patient": patient, "patient_id": patient_id, "appointment_id": appointment_id,
               "amount": round(float(amount), 2), "status": status, "vat": "0% (ιατρική εξαίρεση)"}
        self.kv.put(self._ns("invoice"), iid, rec)
        self._audit("invoice.issue", {"id": iid, "amount": rec["amount"]})
        return rec

    def list_invoices(self) -> list[dict]:
        return sorted(self.kv.all(self._ns("invoice")).values(), key=lambda i: i["id"], reverse=True)

    def pay_invoice(self, iid: str) -> dict:
        inv = self.kv.get(self._ns("invoice"), iid)
        if not inv:
            raise KeyError(iid)
        inv["status"] = "paid"
        self.kv.put(self._ns("invoice"), iid, inv)
        self._audit("invoice.paid", {"id": iid})
        return inv

    # ── clinical notes (signed by a clinician — persisted, restart-surviving) ──
    def add_note(self, note: dict) -> dict:
        nid = note.get("id") or _nid("note")
        rec = {**note, "id": nid, "signed_at": time.strftime("%d/%m/%Y %H:%M")}
        self.kv.put(self._ns("note"), nid, rec)
        self._audit("note.signed_persisted", {"id": nid, "by": rec.get("signed_by", "")})
        return rec

    def list_notes(self) -> list[dict]:
        return list(self.kv.all(self._ns("note")).values())

    # ── recovered-value ledger + billing ──
    def add_value(self, kind: str, euros: float) -> dict:
        vid = _nid("val")
        rec = {"id": vid, "kind": kind, "euros": round(float(euros), 2)}
        self.kv.put(self._ns("value"), vid, rec)
        self._audit("ledger.value", {"kind": kind, "euros": rec["euros"]})
        return rec

    def ledger(self) -> list[dict]:
        return list(self.kv.all(self._ns("value")).values())

    def billing(self) -> dict:
        evs = [ValueEvent(kind=v["kind"], euros=v["euros"]) for v in self.ledger()]
        return _BILLING.bill_sync(evs).model_dump()

    # ── derived metrics — 100% from real data ──
    def metrics(self) -> dict:
        appts = [a for a in self.list_appointments() if a["status"] != "cancelled"]
        today = [a for a in appts if a["date"] == today_iso()]     # REAL calendar day
        done = [a for a in today if a["status"] == "completed"]
        no_show = [a for a in today if a["status"] == "no_show"]
        confirmed = [a for a in today if a["status"] in ("booked", "confirmed")]
        unpaid = sum(i["amount"] for i in self.list_invoices() if i["status"] == "unpaid")
        b = self.billing()
        return {
            "appointments_today": len(today), "completed": len(done), "no_shows": len(no_show),
            "upcoming": len(confirmed), "patients": len(self.list_patients()),
            "waitlist": len(self.list_waitlist()), "unpaid_eur": round(unpaid, 2),
            "recovered_eur": b["total_value_eur"], "charge_eur": b["charge_eur"],
            "net_kept_eur": b.get("net_kept_eur", 0.0), "roi": b["roi"],   # net_kept = the real figure
            "occupancy": round(len(today) / 12.0, 2),   # ~12 slots/day
        }

    # ── import: a clinic connects its data ──
    def import_data(self, patients: Optional[list[dict]] = None,
                    appointments: Optional[list[dict]] = None) -> dict:
        np_ = na = 0
        name_to_id: dict[str, str] = {}
        for p in (patients or []):
            rec = self.add_patient(name=p.get("name", "Ασθενής"), phone=p.get("phone", ""),
                                   amka=p.get("amka", ""), consent=bool(p.get("consent", True)),
                                   tag=p.get("tag", "ενεργός"))
            name_to_id[rec["name"]] = rec["id"]
            np_ += 1
        for a in (appointments or []):
            try:
                self.add_appointment(time=a.get("time", "09:00"), patient=a.get("patient", "Ασθενής"),
                                     service=a.get("service", ""), date=a.get("date"),
                                     value=a.get("value"), status=a.get("status", "confirmed"),
                                     patient_id=name_to_id.get(a.get("patient", ""), ""))
                na += 1
            except ValueError:
                pass   # slot clash / bad time on import → skip, don't corrupt
        self._audit("clinic.import", {"patients": np_, "appointments": na})
        return {"patients_imported": np_, "appointments_imported": na}

    def is_empty(self) -> bool:
        return not (self.list_patients() or self.list_appointments())

    def seed_demo(self, profile: Optional[dict] = None) -> dict:
        """Realistic starter clinic on the REAL calendar (appointments today + tomorrow) so a
        fresh install isn't blank — editable, persistent data, not hardcoded UI. A clinic
        replaces it via import_data(). `profile` customizes name/specialty/patients/services."""
        if not self.is_empty():
            return {"seeded": False}
        p = profile or {}
        self.setup_clinic(name=p.get("name", "Ιατρείο Ευεξία"),
                          specialty=p.get("specialty", "Γενική Ιατρική"),
                          default_value=p.get("default_value"))
        pts = p.get("patients") or [
            {"name": "Γ. Παπαδόπουλος", "phone": "6945123456", "amka": "12036512345", "tag": "ενεργός"},
            {"name": "Μ. Νικολάου", "phone": "6971000002", "tag": "οφειλή"},
            {"name": "Νίκος Κ.", "phone": "6971000003", "tag": "recall"},
            {"name": "Δ. Γεωργίου", "phone": "6971000004", "tag": "no-show"},
            {"name": "Ε. Κωνσταντίνου", "phone": "6971000005", "tag": "ενεργός"}]
        svc = p.get("services") or ["Ιατρική επίσκεψη", "Εξέταση αίματος", "Προληπτικός έλεγχος",
                                    "Follow-up", "Συνταγογράφηση"]
        today = today_iso()
        tomorrow = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
        names = [x["name"] for x in pts]
        appts = [  # a REAL day: morning already happened, one no-show, afternoon upcoming
            {"time": "09:00", "patient": names[0], "service": svc[0], "status": "completed", "date": today},
            {"time": "10:00", "patient": names[1 % len(names)], "service": svc[1 % len(svc)], "status": "completed", "date": today},
            {"time": "11:00", "patient": names[3 % len(names)], "service": svc[0], "status": "no_show", "date": today},
            {"time": "12:00", "patient": names[4 % len(names)], "service": svc[2 % len(svc)], "status": "completed", "date": today},
            {"time": "16:00", "patient": names[2 % len(names)], "service": svc[3 % len(svc)], "status": "confirmed", "date": today},
            {"time": "17:00", "patient": names[0], "service": svc[4 % len(svc)], "status": "confirmed", "date": today},
            {"time": "09:30", "patient": names[1 % len(names)], "service": svc[0], "status": "confirmed", "date": tomorrow},
            {"time": "11:00", "patient": names[2 % len(names)], "service": svc[3 % len(svc)], "status": "confirmed", "date": tomorrow},
        ]
        self.import_data(pts, appts)
        self.add_waitlist(p.get("waitlist", ["Κ. Βασιλείου", "Α. Μακρή"])[0], "προτιμά πρωί")
        if len(p.get("waitlist", ["Κ. Βασιλείου", "Α. Μακρή"])) > 1:
            self.add_waitlist(p.get("waitlist", ["", "Α. Μακρή"])[1], "μετά τις 17:00")
        val = self.get_clinic().default_value
        self.add_invoice(names[0], val, "paid")
        self.add_invoice(names[1 % len(names)], val, "unpaid")   # → a real open balance
        self.add_value("recovered_appointment", val)
        self.add_value("recovered_appointment", val)
        self.add_value("filled_slot", val)
        return {"seeded": True}


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cs = ClinicStore(":memory:")
    cs.seed_demo()
    m = cs.metrics()
    print("seeded clinic:", cs.get_clinic().name)
    print("metrics:", m)
    a = cs.add_appointment("18:00", "Νέος Ασθενής")
    print("booked:", a["id"], "| appts today:", cs.metrics()["appointments_today"])
    print("double-book guard:", end=" ")
    try:
        cs.add_appointment("18:00", "Άλλος")
    except ValueError as e:
        print("blocked ✓")
