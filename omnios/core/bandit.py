"""
bandit.py — Thompson sampling over no-show interventions (learned, not hardcoded, effects).
============================================================================================
Research grounding (verified citations in RESEARCH.md):
  • W.R. Thompson, "On the Likelihood that One Unknown Probability Exceeds Another in View of
    the Evidence of Two Samples", Biometrika 25(3–4):285–294, 1933 — the original algorithm.
  • O. Chapelle & L. Li, "An Empirical Evaluation of Thompson Sampling", NIPS 2011 — the modern
    empirical case for TS as the default bandit baseline.

Why it matters here: the MDP's intervention effects (SMS ×0.75 etc.) are meta-analytic PRIORS —
population averages. Every clinic deviates from the average. Thompson sampling closes that gap:
each intervention arm keeps a Beta posterior over its OWN show-rate in THIS clinic; each decision
samples from the posteriors and picks the EV-max arm; each real outcome updates the chosen arm.
Exploration/exploitation is handled by the posterior width itself — no tuning knobs.

Deterministic under a seeded RNG (so tests and demos are exactly reproducible).
Tests: python core/test_bandit.py
"""
from __future__ import annotations

import random
from typing import Optional

from pydantic import BaseModel

from predictive_billing import _EFFECTS, Action


class ArmState(BaseModel):
    action: str
    alpha: float            # successes (patient SHOWED) + prior
    beta: float             # failures (no-show) + prior
    pulls: int
    posterior_mean_show: float


class ThompsonInterventionPolicy:
    """One Beta(α,β) posterior per intervention arm over P(show | arm), warm-started from the
    published meta-analytic priors (prior strength = 4 pseudo-observations — informative but
    quickly dominated by the clinic's own outcomes)."""

    def __init__(self, base_no_show: float = 0.23, value: float = 40.0,
                 seed: int = 7, prior_strength: float = 4.0) -> None:
        self.rng = random.Random(seed)                        # deterministic by construction
        self.value = value
        self.arms: dict[Action, list[float]] = {}
        self.pulls: dict[Action, int] = {}
        for action, (mult, _cost) in _EFFECTS.items():
            p_show_prior = 1.0 - min(1.0, base_no_show * mult)
            self.arms[action] = [p_show_prior * prior_strength,          # α
                                 (1.0 - p_show_prior) * prior_strength]  # β
            self.pulls[action] = 0

    def _cost(self, action: Action) -> float:
        return _EFFECTS[action][1]

    def choose(self) -> Action:
        """Thompson step: sample p̃_a ~ Beta(α_a, β_a); pick argmax_a p̃_a·value − cost_a."""
        best, best_ev = None, float("-inf")
        for action, (a, b) in self.arms.items():
            p = self.rng.betavariate(a, b)
            ev = p * self.value - self._cost(action)
            if ev > best_ev:
                best, best_ev = action, ev
        return best

    def update(self, action: Action, showed: bool) -> None:
        if showed:
            self.arms[action][0] += 1.0
        else:
            self.arms[action][1] += 1.0
        self.pulls[action] += 1

    def state(self) -> list[ArmState]:
        out = []
        for action, (a, b) in self.arms.items():
            out.append(ArmState(action=action.value, alpha=round(a, 2), beta=round(b, 2),
                                pulls=self.pulls[action],
                                posterior_mean_show=round(a / (a + b), 4)))
        return sorted(out, key=lambda s: -s.posterior_mean_show)


def simulate_clinic(policy: ThompsonInterventionPolicy, true_show: dict[Action, float],
                    rounds: int, seed: int = 11) -> dict:
    """A clinic whose TRUE per-intervention show-rates differ from the published priors.
    Returns the learning trace — proof the policy converges to THIS clinic's best arm."""
    env = random.Random(seed)
    total_shows = 0
    choices: dict[Action, int] = {a: 0 for a in true_show}
    for _ in range(rounds):
        arm = policy.choose()
        showed = env.random() < true_show[arm]
        policy.update(arm, showed)
        choices[arm] += 1
        total_shows += showed
    best_arm = max(true_show, key=lambda a: true_show[a] * 1.0 - _EFFECTS[a][1] / policy.value)
    return {"rounds": rounds, "total_shows": total_shows,
            "choices": {a.value: c for a, c in choices.items()},
            "best_arm": best_arm.value,
            "best_arm_share_last_half": None,   # filled by a second pass below when needed
            "posteriors": [s.model_dump() for s in policy.state()]}


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    # a clinic where (unlike the population prior) the AI call clearly beats SMS
    true_show = {Action.NONE: 0.72, Action.SMS: 0.80, Action.AI_CALL: 0.93, Action.DEPOSIT: 0.84}
    pol = ThompsonInterventionPolicy(seed=7)
    r = simulate_clinic(pol, true_show, rounds=1500, seed=11)
    print("true best arm:", r["best_arm"], "| choices:", r["choices"])
    print("top posterior:", r["posteriors"][0]["action"],
          "mean-show", r["posteriors"][0]["posterior_mean_show"])
