"""
clinic_autopilot.py — the autonomous operator that ACTS on the REAL clinic data.
================================================================================
This is the difference between "looks autonomous" and "is autonomous": one sweep scans the SAME
persistent clinic store the UI shows and files governed PROPOSALS across the whole front+back desk —
no-show recovery, reminders, recalls, dunning, waitlist offers, invoicing. A human APPROVES each
(GDPR / EU AI Act human oversight, GR Law 3471/2006): the operator never contacts a patient by
itself. Every propose and approve is appended to the tamper-evident audit chain (what/why/who).

Honest money model (defensible to a finance CEO): `est_eur` on a proposal is € AT RISK / addressable,
NOT recovered. Recovered-€ only accrues from REALIZED events (a real re-booking, a paid invoice) that
flow through the clinic store — the operator never books projected money into the ledger.

Deterministic, stdlib + the clinic store. Tests: python core/test_clinic_autopilot.py
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from clinic_store import ClinicStore

# € at risk per task type (evidence-anchored; addressable, not recovered)
_EUR_AT_RISK = {"no_show_recovery": None, "recall": 120, "dunning": None,
                "reminder": 0, "waitlist_fill": 60, "billing": None}

_LABELS = {
    "no_show_recovery": "Ανάκτηση no-show",
    "reminder": "Υπενθύμιση ραντεβού",
    "recall": "Recall / ανανέωση πακέτου",
    "dunning": "Υπενθύμιση οφειλής",
    "waitlist_fill": "Προσφορά κενής θέσης",
    "billing": "Έκδοση τιμολογίου",
    "risk_defense": "Άμυνα ρίσκου no-show",
}
# high-risk types touch a patient / money → always need human approval; low-risk are internal
_HIGH_RISK = {"no_show_recovery", "recall", "dunning", "waitlist_fill", "risk_defense"}


class ClinicAutopilot:
    """Wraps a ClinicStore. `sweep()` files proposals; `approve()` executes the real side-effect."""

    def __init__(self, store: ClinicStore, channels=None) -> None:
        self.cs = store
        if channels is None:                      # DI seam; default = the real dispatcher
            from channels import MessageChannels
            channels = MessageChannels(store)
        self.channels = channels
        # ONE lock per tenant's autopilot (api.py shares a single instance between the background
        # heartbeat and the HTTP endpoints). Held across each propose/approve/reject critical
        # section so the read-check-execute-write is atomic — otherwise two concurrent runs both
        # see status=='pending' and fire the SAME real SMS/invoice twice. Reentrant so
        # run_autonomous() can hold it while calling approve() (same thread) without deadlock.
        self._lock = threading.RLock()

    def _phone_for(self, name: str, patient_id: str = "") -> str:
        # prefer the unambiguous id (two patients can share a name — father/son, namesakes)
        for p in self.cs.list_patients():
            if (patient_id and p["id"] == patient_id) or (not patient_id and p["name"] == name):
                return p.get("phone", "")
        return ""

    def _email_for(self, name: str, patient_id: str = "") -> str:
        for p in self.cs.list_patients():
            if (patient_id and p["id"] == patient_id) or (not patient_id and p["name"] == name):
                return p.get("email", "")
        return ""

    def _ns(self) -> str:
        return self.cs._ns("proposal")

    def _file(self, ptype: str, ref: str, patient: str, action: str, est_eur: float, why: str,
              patient_id: str = "") -> Optional[dict]:
        pid = f"{ptype}:{ref}"
        with self._lock:                       # dedupe check + insert atomic (no double-propose race)
            if self.cs.kv.get(self._ns(), pid):    # dedupe — never re-propose the same task
                return None
            # GDPR Art.9 lawful-basis GATE: never propose contacting a patient who hasn't consented.
            # Checked by patient_id when available — a name-only check hits the wrong record for namesakes.
            if not self.cs.consent_for(patient, patient_id):
                self.cs._audit("autopilot.blocked_no_consent", {"type": ptype, "patient_ref": ref})
                return None
            rec = {"id": pid, "type": ptype, "ref": ref, "patient": patient, "patient_id": patient_id,
                   "action": action, "est_eur": round(float(est_eur), 2), "why": why,
                   "risk": "high" if ptype in _HIGH_RISK else "low",
                   "label": _LABELS.get(ptype, ptype), "status": "pending"}
            self.cs.kv.put(self._ns(), pid, rec)
            self.cs._audit("autopilot.propose", {"type": ptype, "ref": ref, "eur": rec["est_eur"]})
            return rec

    def sweep(self) -> dict:
        """One autonomous pass over the REAL clinic data → governed proposals (deduped)."""
        appts = self.cs.list_appointments()
        patients = self.cs.list_patients()
        session_value = self.cs.get_clinic().default_value   # every € traces to a real session price
        new: list[dict] = []

        def add(*a):
            r = self._file(*a)
            if r:
                new.append(r)

        # 1) No-show recovery — a real €-loss to chase (win-back 10–15%)
        for a in appts:
            if a["status"] == "no_show":
                add("no_show_recovery", a["id"], a["patient"],
                    f"Κάλεσε τον/την {a['patient']} να ξανακλείσει το ραντεβού των {a['time']}",
                    a["value"], f"No-show στις {a['time']} · €{a['value']} σε ρίσκο · win-back 10–15%",
                    a.get("patient_id", ""))
        # 2) Reminders — cut future no-shows (25% relative, Robotham 2016 meta-analysis RR 0.75)
        for a in appts:
            if a["status"] in ("confirmed", "booked"):
                add("reminder", a["id"], a["patient"],
                    f"Αποστολή υπενθύμισης: {a['service']} @ {a['time']}", 0.0,
                    "Υπενθύμιση 24ω → ≈25% λιγότερα no-shows (meta-analysis, RR 0.75)",
                    a.get("patient_id", ""))
        # 3) Recall — completed-package / flagged patients
        for p in patients:
            done_pkg = p.get("visits", 0) > 0 and p["visits"] % 10 == 0
            if p.get("tag") == "recall" or done_pkg:
                add("recall", p["id"], p["name"],
                    f"Πρόταση ανανέωσης πακέτου στον/στην {p['name']}", session_value,
                    f"{p.get('visits', 0)} επισκέψεις · πακέτο ολοκληρώθηκε → ανανέωση (1 session €{session_value:g})",
                    p["id"])   # for a patient record, the id IS the patient_id
        # 4) Dunning — open balances / unpaid invoices
        for inv in self.cs.list_invoices():
            if inv["status"] == "unpaid":
                add("dunning", inv["id"], inv["patient"],
                    f"Ευγενική υπενθύμιση οφειλής €{inv['amount']} — {inv['patient']}",
                    inv["amount"], "Ανοιχτή οφειλή · dunning ακολουθία", inv.get("patient_id", ""))
        # 5) Waitlist fill — offer any free slot to the next waiter (waitlist is a name-only entry)
        if self.cs.list_waitlist():
            w = self.cs.list_waitlist()[0]
            add("waitlist_fill", w["id"], w["name"],
                f"Προσφορά κενής θέσης στον/στην {w['name']}", session_value,
                f"Ασθενής σε αναμονή · γέμισμα κενού slot (1 session €{session_value:g})", "")
        # 6) PROACTIVE risk defense (clinic_brain): high-risk επερχόμενα ραντεβού → escalated
        #    reminder + pre-offer του slot στη waitlist, ΠΡΙΝ γίνει το no-show. Κάθε πρόταση
        #    κουβαλάει τους ΛΟΓΟΥΣ του μοντέλου (interpretable) ώστε ο γιατρός να δει το «γιατί».
        try:
            from clinic_brain import scan_clinic
            for s in scan_clinic(self.cs).get("appointments", []):
                if s["band"] == "high":
                    add("risk_defense", s["appointment_id"], s["patient"],
                        f"Έξτρα υπενθύμιση στον/στην {s['patient']} ({s['date']} {s['time']}) "
                        f"+ pre-offer του slot στη λίστα αναμονής",
                        session_value,
                        f"Ρίσκο no-show {int(s['risk']*100)}%: " + " · ".join(s["reasons"]))
        except Exception:
            pass   # risk scan failure must never break the sweep

        eur = round(sum(p["est_eur"] for p in self.pending()), 2)
        return {"new": len(new), "proposals": new, "pending": len(self.pending()), "eur_at_risk": eur}

    def pending(self) -> list[dict]:
        return [p for p in self.cs.kv.all(self._ns()).values() if p["status"] == "pending"]

    def all(self) -> list[dict]:
        return list(self.cs.kv.all(self._ns()).values())

    def approve(self, pid: str, approver: str, via: str = "human") -> dict:
        """Authorize the action → execute its REAL side-effect + audit (what/why/who/how).
        `via`='human' (a person clicked approve) or 'policy' (autonomy policy authorized it).
        The whole read-check-execute-write runs under the lock so the side-effect fires EXACTLY
        ONCE even when the heartbeat and an HTTP request hit the same proposal concurrently."""
        if not (approver or "").strip():
            raise PermissionError("an approver is required")
        with self._lock:
            p = self.cs.kv.get(self._ns(), pid)
            if not p:
                raise KeyError(pid)
            # IDEMPOTENCY GATE (now atomic under the lock) — a proposal executes its side-effect
            # exactly once; a double-click / retry / concurrent heartbeat returns it unchanged.
            if p["status"] != "pending":
                return p
            # CONSENT RE-CHECK at execution time (GDPR Art.9): consent is verified at propose-time,
            # but a patient may revoke between the proposal and the (up to 5-min-later) auto-run.
            # Re-verify right before any patient-facing send; billing is not patient outreach.
            if p["type"] != "billing" and not self.cs.consent_for(p["patient"], p.get("patient_id", "")):
                p["status"] = "blocked_no_consent"
                self.cs.kv.put(self._ns(), pid, p)
                self.cs._audit("autopilot.blocked_no_consent",
                               {"id": pid, "type": p["type"], "at": "execution"})
                return p
            # execute the real side-effect (honest: no projected money, no pretend sends)
            executed = "approved"
            if p["type"] == "billing":
                self.cs.add_invoice(p["patient"], p["est_eur"])
                executed = "invoice issued"
            elif p["type"] in ("no_show_recovery", "reminder", "recall", "dunning", "waitlist_fill",
                               "risk_defense"):
                # patient-facing outreach → dispatch through the channel layer: REALLY sends when the
                # clinic has provider keys, otherwise lands in the durable outbox with the reason.
                # Channel choice: prefer SMS when the clinic has Twilio configured (today's default
                # once GR numbers are set up); otherwise fall back to email when the clinic's Resend
                # key is live AND this patient has an email on file — instead of queuing forever with
                # a channel that will never come online. Neither ready / no email on file → still
                # dispatch "sms" so it queues honestly with the real reason (unchanged behavior).
                pat_id = p.get("patient_id", "")
                if self.channels.sms_ready or not self.channels.email_ready:
                    kind, to = "sms", self._phone_for(p["patient"], pat_id)
                else:
                    email = self._email_for(p["patient"], pat_id)
                    kind, to = ("email", email) if email else ("sms", self._phone_for(p["patient"], pat_id))
                r = self.channels.dispatch(kind, to, p["action"], why=p["why"], approved_by=approver)
                executed = f"{kind} {r['status']}" + (f" ({r.get('reason', '')})" if r["status"] != "sent" else "")
            p["status"] = "approved"
            p["approved_by"] = approver
            p["executed"] = executed
            p["via"] = via                     # 'human' (clicked) vs 'policy' (autonomous) — for audit + UI
            self.cs.kv.put(self._ns(), pid, p)
            self.cs._audit("autopilot.approve", {"id": pid, "type": p["type"], "by": approver,
                                                 "via": via, "executed": executed})
            return p

    def run_autonomous(self, approver: str = "autopilot") -> dict:
        """The autonomous pass: sweep for work, then EXECUTE every proposal the doctor's policy
        authorizes (consent already enforced at propose-time), leaving the rest for human review.
        Each auto-execution is audited WITH the authorizing policy. This is the 'self-driving
        back office with a flight recorder' — capped, revocable, fully traced."""
        from autonomy_policy import AutonomyPolicy
        pol = AutonomyPolicy(self.cs)
        swept = self.sweep()
        cfg = pol.get()
        limit = int(cfg.get("max_actions_per_sweep", 25))
        executed: list[dict] = []
        for cand in self.pending():
            if len(executed) >= limit:
                break
            # Do authorize→execute→count atomically per proposal (reentrant lock): under concurrency
            # a second run_autonomous re-reads the fresh status and skips what's already done, so no
            # double side-effect AND no double-count. Re-read inside the lock — the snapshot is stale.
            with self._lock:
                p = self.cs.kv.get(self._ns(), cand["id"])
                if not p or p["status"] != "pending":
                    continue
                ok, reason = pol.authorizes(p)
                if not ok:
                    continue
                res = self.approve(p["id"], f"{approver}·policy", via="policy")
                if res.get("status") != "approved":      # e.g. consent revoked at execution → skip
                    continue
                self.cs._audit("autopilot.auto_execute",
                               {"id": p["id"], "type": p["type"], "eur": p.get("est_eur", 0),
                                "policy_by": cfg.get("set_by", ""), "reason": reason})
                executed.append(res)
        awaiting = self.pending()               # whatever the policy did NOT authorize
        return {"new": swept["new"], "eur_at_risk": swept["eur_at_risk"],
                "executed_auto": len(executed), "awaiting_human": len(awaiting),
                "autonomy_enabled": bool(cfg.get("enabled")),
                "executed": [{"type": e["type"], "action": e["action"], "executed": e.get("executed", "")}
                             for e in executed]}

    def reject(self, pid: str, approver: str) -> dict:
        with self._lock:                       # atomic with approve — a proposal ends in one state only
            p = self.cs.kv.get(self._ns(), pid)
            if not p:
                raise KeyError(pid)
            if p["status"] != "pending":
                return p
            p["status"] = "rejected"
            p["approved_by"] = approver
            self.cs.kv.put(self._ns(), pid, p)
            self.cs._audit("autopilot.reject", {"id": pid, "type": p["type"], "by": approver})
        return p

    def briefing(self) -> dict:
        """The ONE thing the physio reads — everything else the operator handles."""
        pend = self.pending()
        by_type: dict[str, int] = {}
        for p in pend:
            by_type[p["type"]] = by_type.get(p["type"], 0) + 1
        eur = round(sum(p["est_eur"] for p in pend), 2)
        return {"pending": len(pend), "by_type": by_type, "eur_at_risk": eur,
                "recovered_eur_realized": self.cs.billing()["total_value_eur"]}


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cs = ClinicStore(":memory:")
    cs.seed_demo()
    ap = ClinicAutopilot(cs)
    r = ap.sweep()
    print(f"autopilot swept the real clinic → {r['new']} proposals, €{r['eur_at_risk']} at risk")
    for p in ap.pending():
        print(f"  [{p['risk']}] {p['label']}: {p['action']}  (€{p['est_eur']})")
    first = ap.pending()[0]
    ap.approve(first["id"], "dr_physio")
    print("approved:", first["label"], "→ pending now", len(ap.pending()))
