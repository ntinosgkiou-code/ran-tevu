"""
autonomous_loop.py — ο ΑΥΤΟΝΟΜΟΣ ΒΡΟΧΟΣ που ΜΑΘΑΙΝΕΙ: τρέχει μόνος του πάνω σε νέα δεδομένα, σε layers.
================================================================================
Το autonomous_agents δίνει ΠΡΟΛΗΠΤΙΚΗ εκτέλεση. Αυτό προσθέτει τη ΜΑΘΗΣΗ: κάθε κύκλος παρατηρεί τα ΝΕΑ
πραγματικά outcomes, ενημερώνει το μοντέλο no-show (Beta-Bernoulli → κινείται προς τον ΑΛΗΘΙΝΟ ρυθμό
του ιατρείου), και τροφοδοτεί το ενημερωμένο μοντέλο πίσω σε κάθε επόμενη απόφαση. Το μαθημένο state
ΕΠΙΒΙΩΝΕΙ restart (persisted στο store). Πέντε επίπεδα:

  L1 INGEST     — βρες τα ΝΕΑ outcomes (completed/no_show) από το τελευταίο watermark.
  L2 LEARN      — Bayes-update το no-show estimate· persist. (synthesize πάνω σε νέα δεδομένα)
  L3 DECIDE     — για τα επερχόμενα, με το ΜΑΘΗΜΕΝΟ rate, ποια χρειάζονται παρέμβαση (PredictiveSimulator).
  L4 EXECUTE    — αυτόνομη εκτέλεση της εξουσιοδοτημένης δουλειάς (AutonomyOrchestrator, policy+consent).
  L5 AUDIT      — κατέγραψε τι έμαθε (rate before→after) + τι εκτέλεσε· tamper-evident.

Idempotent: το watermark εγγυάται ότι κάθε outcome μετράει ΜΙΑ φορά· ένας κύκλος χωρίς νέα δεδομένα
δεν αλλάζει τίποτα. Deterministic, offline. Tests: python core/test_autonomous_loop.py
"""
from __future__ import annotations

from typing import Optional

from agentic_loop import OutcomeLearner
from predictive_billing import Action, AppointmentState, PredictiveSimulator


class AutonomousLoop:
    """Ένας αυτο-μαθαινόμενος βρόχος ανά ιατρείο. `cycle()` = ένα πλήρες πέρασμα των 5 layers."""

    def __init__(self, store, autopilot=None, bus=None) -> None:
        self.cs = store
        if autopilot is None:
            from clinic_autopilot import ClinicAutopilot
            autopilot = ClinicAutopilot(store)
        self.autopilot = autopilot
        from autonomous_agents import AutonomyOrchestrator
        self.orch = AutonomyOrchestrator(store, autopilot, bus=bus)
        self.sim = PredictiveSimulator()
        self.learner = self._load_learner()

    # ── persistence (το μαθημένο state επιβιώνει restart) ──
    def _ns(self) -> str:
        return self.cs._ns("autoloop")

    def _load_learner(self) -> OutcomeLearner:
        d = self.cs.kv.get(self._ns(), "learner")
        return OutcomeLearner.from_dict(d) if d else OutcomeLearner()

    def _observed(self) -> set:
        return set(self.cs.kv.get(self._ns(), "observed") or [])

    def cycle(self) -> dict:
        # ── L1 INGEST: νέα outcomes από το watermark ────────────────────────
        observed = self._observed()
        rate_before = self.learner.no_show_prob()
        new_obs = 0
        for a in self.cs.list_appointments():
            if a["status"] in ("completed", "no_show") and a["id"] not in observed:
                # ── L2 LEARN: Bayes-update πάνω στο ΝΕΟ δεδομένο ─────────────
                self.learner.observe(a["status"] == "no_show")
                observed.add(a["id"])
                new_obs += 1
        if new_obs:
            self.cs.kv.put(self._ns(), "learner", self.learner.to_dict())
            self.cs.kv.put(self._ns(), "observed", sorted(observed))
        rate_after = self.learner.no_show_prob()

        # ── L3 DECIDE: με το ΜΑΘΗΜΕΝΟ rate, ποια επερχόμενα θέλουν παρέμβαση ──
        upcoming = [a for a in self.cs.list_appointments() if a["status"] in ("confirmed", "booked")]
        interventions = 0
        for a in upcoming:
            s = self.sim.simulate_sync(
                AppointmentState(appointment_value=float(a.get("value", 0) or 0), base_no_show_prob=rate_after))
            if s.recommended != Action.NONE:
                interventions += 1

        # ── L4 EXECUTE: αυτόνομη εκτέλεση εξουσιοδοτημένης δουλειάς ──────────
        tick = self.orch.tick()

        # ── L5 AUDIT: τι έμαθε + τι εκτέλεσε ────────────────────────────────
        self.cs._audit("autoloop.cycle",
                       {"observed_new": new_obs, "rate_before": round(rate_before, 4),
                        "rate_after": round(rate_after, 4), "n": self.learner.n,
                        "interventions": interventions, "dispatched": tick["dispatched"]})
        return {"observed_new": new_obs,
                "learned_no_show_before": round(rate_before, 4),
                "learned_no_show_after": round(rate_after, 4),
                "total_observations": self.learner.n,
                "interventions_recommended": interventions,
                "dispatched": tick["dispatched"], "awaiting_human": tick["awaiting_human"],
                "autonomy_enabled": tick["autonomy_enabled"], "swept": tick["swept"]}

    def state(self) -> dict:
        """Το τρέχον μαθημένο state (για UI/report)."""
        return {"learned_no_show_prob": round(self.learner.no_show_prob(), 4),
                "observations": self.learner.n, "prior": 0.23}
