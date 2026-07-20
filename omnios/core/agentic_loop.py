"""
agentic_loop.py — the closed agentic loop: INGEST → DECIDE → ACT → UPDATE/IMPROVE → AUDIT.
=========================================================================================
This is what turns the pieces into an *agent*: it does not just answer — it OBSERVES outcomes and
IMPROVES. The missing link was UPDATE/IMPROVE: an online learner whose no-show estimate moves toward
the clinic's true rate as real outcomes arrive, feeding back into every future DECIDE.

  • INGEST  — appointments, outcomes, documents.
  • DECIDE  — PredictiveSimulator over the CURRENT learned no-show rate → best intervention.
  • ACT     — propose the intervention (HITL for irreversible), record the intent.
  • UPDATE  — observe the outcome, Bayes-update the learner (improve), attribute recovered €.
  • AUDIT   — every step appended to the tamper-evident hash-chained log (EU AI Act Art.12),
              and a human-readable report() so the whole thing is understandable.

Deterministic, offline, Pydantic v2, typed. Tests: python core/test_agentic_loop.py
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from audit import AUDIT
from predictive_billing import AppointmentState, PredictiveSimulator
from store import KVStore


class OutcomeLearner:
    """Online Beta-Bernoulli estimate of the no-show rate. Starts at a prior and MOVES toward the
    clinic's true rate as outcomes arrive — the 'improve' in the loop. Swap for a richer model
    (features/contextual bandit) behind the same interface without touching the agent."""

    def __init__(self, prior_no_show: float = 0.23, strength: float = 10.0) -> None:
        self.alpha = prior_no_show * strength          # pseudo-count of no-shows
        self.beta = (1.0 - prior_no_show) * strength   # pseudo-count of shows
        self.n = 0

    def no_show_prob(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def observe(self, no_show: bool) -> None:
        if no_show:
            self.alpha += 1.0
        else:
            self.beta += 1.0
        self.n += 1

    def to_dict(self) -> dict:
        return {"alpha": self.alpha, "beta": self.beta, "n": self.n}

    @classmethod
    def from_dict(cls, d: dict) -> "OutcomeLearner":
        o = cls()
        o.alpha, o.beta, o.n = d["alpha"], d["beta"], d["n"]
        return o


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)
    appointment_id: str
    no_show_prob_used: float
    recommended: str
    predicted_no_show: float
    expected_value_gain: float


class LoopReport(BaseModel):
    learned_no_show_prob: float
    observations: int
    recovered_eur: float
    decisions: int
    audit_records: int
    audit_chain_intact: bool
    audit_tail: list[dict]


class AgenticClinicAgent:
    def __init__(self, learner: Optional[OutcomeLearner] = None,
                 simulator: Optional[PredictiveSimulator] = None) -> None:
        self.learner = learner or OutcomeLearner()
        self.simulator = simulator or PredictiveSimulator()
        self._appts: dict[str, dict] = {}
        self._decisions: list[Decision] = []
        self.recovered_eur = 0.0

    def ingest(self, event: dict) -> dict:
        etype = event.get("type")
        if etype == "appointment":
            return self._on_appointment(event)
        if etype == "outcome":
            return self._on_outcome(event)
        raise ValueError(f"unknown event type: {etype!r}")

    # DECIDE + ACT
    def _on_appointment(self, e: dict) -> dict:
        appt_id = str(e["id"])
        p = self.learner.no_show_prob()                       # uses the CURRENT learned rate
        sim = self.simulator.simulate_sync(
            AppointmentState(appointment_value=float(e["value"]), base_no_show_prob=p))
        predicted_ns = sim.predicted_next_state["NO_SHOW"]
        self._appts[appt_id] = {"value": float(e["value"]), "recommended": sim.recommended.value,
                                "predicted_no_show": predicted_ns}
        dec = Decision(appointment_id=appt_id, no_show_prob_used=round(p, 4),
                       recommended=sim.recommended.value, predicted_no_show=round(predicted_ns, 4),
                       expected_value_gain=sim.expected_value_gain)
        self._decisions.append(dec)
        AUDIT.record("agent.decide", {"appt": appt_id, "no_show_prob": round(p, 4),
                                      "recommend": sim.recommended.value,
                                      "eur_gain": sim.expected_value_gain})   # ACT = propose (audited)
        return dec.model_dump()

    # UPDATE / IMPROVE
    def _on_outcome(self, e: dict) -> dict:
        appt_id = str(e["id"])
        showed = bool(e["showed"])
        no_show = not showed
        self.learner.observe(no_show)                         # <-- the improvement step
        appt = self._appts.get(appt_id, {})
        recovered = 0.0
        if showed and appt.get("recommended") not in (None, "none"):
            # an at-risk appointment that showed after we intervened → attribute the € we expected to lose
            recovered = round(appt.get("value", 0.0) * appt.get("predicted_no_show", 0.0), 2)
            self.recovered_eur = round(self.recovered_eur + recovered, 2)
        AUDIT.record("agent.update", {"appt": appt_id, "showed": showed,
                                      "new_no_show_prob": round(self.learner.no_show_prob(), 4),
                                      "recovered_eur": recovered})
        return {"learned_no_show_prob": round(self.learner.no_show_prob(), 4),
                "recovered_eur": self.recovered_eur, "observations": self.learner.n}

    # PERSISTENCE — the brain survives a restart (system of record)
    def save(self, store: KVStore, key: str = "default") -> None:
        store.put("agentic", key, {"learner": self.learner.to_dict(),
                                   "recovered_eur": self.recovered_eur, "appts": self._appts})

    def load(self, store: KVStore, key: str = "default") -> "AgenticClinicAgent":
        d = store.get("agentic", key)
        if d:
            self.learner = OutcomeLearner.from_dict(d["learner"])
            self.recovered_eur = d["recovered_eur"]
            self._appts = d.get("appts", {})
        return self

    # AUDIT / UNDERSTANDABLE
    def report(self) -> LoopReport:
        return LoopReport(
            learned_no_show_prob=round(self.learner.no_show_prob(), 4),
            observations=self.learner.n, recovered_eur=self.recovered_eur,
            decisions=len(self._decisions), audit_records=len(AUDIT),
            audit_chain_intact=AUDIT.verify(), audit_tail=AUDIT.tail(6),
        )


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    agent = AgenticClinicAgent()
    print("start: learned no-show prob =", round(agent.learner.no_show_prob(), 3))
    # DECIDE on 3 appointments
    for i in range(3):
        d = agent.ingest({"type": "appointment", "id": f"A{i}", "value": 45})
    # this clinic's TRUE no-show rate is ~0.5 — feed real outcomes and watch the agent IMPROVE
    outcomes = [True, False, True, False, True, False, True, False, False, True] * 3
    for i, showed in enumerate(outcomes):
        agent.ingest({"type": "appointment", "id": f"B{i}", "value": 45})
        agent.ingest({"type": "outcome", "id": f"B{i}", "showed": showed})
    r = agent.report()
    print("after learning: no-show prob =", r.learned_no_show_prob,
          f"(moved from 0.23 toward the true ~0.5 over {r.observations} outcomes)")
    print("recovered €:", r.recovered_eur, "| decisions:", r.decisions,
          "| audit records:", r.audit_records, "| chain intact:", r.audit_chain_intact)
