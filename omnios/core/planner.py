"""
planner.py — ReAct re-planner (Observe → Reason → Act) for self-healing.
=======================================================================
On a verified conflict the cognitive loop OBSERVES the real free slots (a tool
over live state), REASONS over the user's extracted preference (morning /
afternoon / evening / asap / closest-to-requested), and ACTS by choosing the
best candidate. Deterministic and offline by default; an optional Claude path
refines the choice when ANTHROPIC_API_KEY is set. Bounded by the caller.
"""
from __future__ import annotations

import json
import os
from typing import List, Optional, Tuple

_WINDOWS = {"morning": 10 * 60, "afternoon": 14 * 60, "evening": 19 * 60}


def _mins(slot: str) -> int:
    return int(slot[11:13]) * 60 + int(slot[14:16])


def _deterministic(pref: Optional[str], candidates: List[str], original: str) -> str:
    if pref == "asap":
        return sorted(candidates, key=_mins)[0]
    if pref in _WINDOWS:
        target = _WINDOWS[pref]
        return sorted(candidates, key=lambda c: (abs(_mins(c) - target), _mins(c)))[0]
    o = _mins(original)
    return sorted(candidates, key=lambda c: (abs(_mins(c) - o), _mins(c)))[0]


def _llm_rank(intent_text: str, candidates: List[str]) -> Optional[str]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-opus-4-8", max_tokens=80,
            system=("Pick the single best appointment slot for the user's intent. "
                    "Reply with ONLY the chosen slot string, exactly as given."),
            messages=[{"role": "user", "content":
                       f"intent: {intent_text}\ncandidates: {json.dumps(candidates)}"}])
        choice = "".join(getattr(b, "text", "") for b in msg.content).strip()
        return choice if choice in candidates else None
    except Exception:
        return None


def plan(frame, candidates: List[str], original: str) -> Tuple[Optional[str], str]:
    """Return (chosen_slot, human-readable reasoning)."""
    if not candidates:
        return None, "no free slots within the search window"
    pref = (getattr(frame, "constraints", {}) or {}).get("time_pref")

    llm_choice = _llm_rank(f"{getattr(frame,'object','')} pref={pref}", candidates)
    if llm_choice:
        return llm_choice, f"LLM ranked {len(candidates)} options → {llm_choice[11:]} (pref={pref})"

    chosen = _deterministic(pref, candidates, original)
    label = pref or "closest-to-requested"
    return chosen, f"preference={label} over {len(candidates)} free slots → {chosen[11:]}"
