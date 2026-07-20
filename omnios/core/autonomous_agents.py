"""
autonomous_agents.py — οι ΑΥΤΟΝΟΜΟΙ agents: δρουν ΠΡΟΛΗΠΤΙΚΑ, όχι μόνο αντιδραστικά.
================================================================================
Το clinic_bridge έχει agents που ΑΝΤΙΔΡΟΥΝ σε ένα αίτημα ασθενή (booking→...→checkout). Αυτό εδώ
προσθέτει την ΠΡΟΛΗΠΤΙΚΗ πλευρά: ένα `tick()` σαρώνει τα ΠΡΑΓΜΑΤΙΚΑ δεδομένα του ιατρείου και στέλνει
δουλειά στο ΙΔΙΟ event bus, όπου εξειδικευμένοι agents την αναλαμβάνουν και την ΕΚΤΕΛΟΥΝ.

Αρχιτεκτονική (η σωστή διαίρεση ρόλων):
  • ClinicAutopilot = ο ΕΓΚΕΦΑΛΟΣ — αποφασίζει ΤΙ χρειάζεται (sweep → proposals με λόγο & €-at-risk).
  • ClinicEventBus  = το ΝΕΥΡΙΚΟ ΣΥΣΤΗΜΑ — οι agents επικοινωνούν, κάθε event στο audit.
  • SpecialistAgent = τα ΧΕΡΙΑ — εκτελούν μέσω του ΑΣΦΑΛΟΥΣ approve() (idempotent, consent-gated,
                       policy-authorized, audited) και αναφέρουν το αποτέλεσμα πίσω στο bus.

Έτσι οι reactive (journey) + proactive (autonomous) agents μοιράζονται ΕΝΑ bus → ένα ενοποιημένο,
πλήρως ιχνηλατήσιμο «αυτο-οδηγούμενο back office». Καμία διπλή εκτέλεση: το approve() είναι idempotent
και το sweep() κάνει dedupe. Deterministic. Tests: python core/test_autonomous_agents.py
"""
from __future__ import annotations

from typing import Optional

# proposal type → (bus event, ελληνική ετικέτα υπαλλήλου)
WORK_EVENTS = {
    "no_show_recovery": ("work.recovery", "Ανάκτηση no-show"),
    "reminder":         ("work.reminder", "Υπενθυμίσεις"),
    "recall":           ("work.recall",   "Recall/ανανέωση"),
    "dunning":          ("work.dunning",  "Οφειλές"),
    "waitlist_fill":    ("work.waitlist", "Κενές θέσεις"),
    "risk_defense":     ("work.risk",     "Άμυνα ρίσκου"),
    "billing":          ("work.billing",  "Τιμολόγηση"),
}


class SpecialistAgent:
    """Ένας εξειδικευμένος υπάλληλος ανά τύπο εργασίας. Ακούει το work.<type> event του, εκτελεί την
    εγκεκριμένη ενέργεια μέσω του autopilot.approve() (η ΜΟΝΗ, ασφαλής, idempotent+consent οδός),
    και δημοσιεύει το αποτέλεσμα (work.<type>.done) πίσω στο bus ώστε να φανεί στη ροή + audit."""

    def __init__(self, bus, autopilot, ptype: str, event: str, label: str) -> None:
        self.bus, self.ap, self.ptype, self.event, self.label = bus, autopilot, ptype, event, label
        self.name = f"agent:{ptype}"
        bus.subscribe(event, self.name, self.on_work)

    def on_work(self, p: dict) -> None:
        res = self.ap.approve(p["pid"], approver=self.name, via="policy")   # idempotent + consent re-check
        self.bus.publish(f"{self.event}.done",
                         {"pid": p["pid"], "type": self.ptype, "status": res.get("status"),
                          "executed": res.get("executed", ""), "ref": p["pid"]}, source=self.name)


class AutonomyOrchestrator:
    """Συνδέει τον αυτόνομο autopilot με το event bus. `tick()` = μία προληπτική σάρωση: βρες δουλειά,
    στείλε στο bus ΜΟΝΟ ό,τι επιτρέπει η πολιτική του γιατρού, οι agents την εκτελούν & αναφέρουν."""

    def __init__(self, store, autopilot, bus=None) -> None:
        self.cs = store
        self.ap = autopilot
        if bus is None:
            from clinic_bridge import ClinicEventBus
            bus = ClinicEventBus()
        self.bus = bus
        self.agents = {ptype: SpecialistAgent(bus, autopilot, ptype, event, label)
                       for ptype, (event, label) in WORK_EVENTS.items()}

    def tick(self, approver: str = "autonomy") -> dict:
        """Μία προληπτική σάρωση. Σέβεται την πολιτική αυτονομίας (enabled + ανά-τύπο + πλαφόν €) και
        το max_actions_per_sweep. Ό,τι ΔΕΝ εξουσιοδοτεί η πολιτική μένει για τον γιατρό (δεν στέλνεται)."""
        from autonomy_policy import AutonomyPolicy
        pol = AutonomyPolicy(self.cs)
        cfg = pol.get()
        swept = self.ap.sweep()                    # deduped — δεν ξανα-προτείνει ό,τι υπάρχει
        limit = int(cfg.get("max_actions_per_sweep", 25))
        dispatched, skipped_policy = 0, 0
        if cfg.get("enabled"):
            for cand in self.ap.pending():
                if dispatched >= limit:
                    break
                ok, _reason = pol.authorizes(cand)
                if not ok:
                    skipped_policy += 1
                    continue                       # όχι εξουσιοδοτημένο → μένει για human review
                spec = WORK_EVENTS.get(cand["type"])
                if not spec:
                    continue
                event = spec[0]
                self.bus.publish(event,
                                 {"pid": cand["id"], "type": cand["type"], "patient": cand["patient"],
                                  "action": cand["action"], "why": cand["why"], "ref": cand["id"]},
                                 source="autonomy")
                dispatched += 1
        awaiting = len(self.ap.pending())          # ό,τι απέμεινε (μη-εξουσιοδοτημένο ή consent-blocked)
        return {"swept": swept["new"], "eur_at_risk": swept["eur_at_risk"],
                "autonomy_enabled": bool(cfg.get("enabled")),
                "dispatched": dispatched, "skipped_policy": skipped_policy, "awaiting_human": awaiting}

    def feed(self, limit: int = 30) -> list[dict]:
        """Τι έκανε ο κάθε agent στο τελευταίο tick — για UI live feed. Μόνο τα work.* events."""
        labels = {ptype: label for ptype, (_e, label) in WORK_EVENTS.items()}
        out = []
        for e in self.bus.journey():
            if e["type"].startswith("work."):
                pl = e["payload"]
                out.append({"seq": e["seq"], "event": e["type"], "source": e["source"],
                            "agent": labels.get(pl.get("type"), pl.get("type", "")),
                            "status": pl.get("status"), "executed": pl.get("executed", "")})
        return out[-limit:]
