"""
nlu.py — Semantic Frame parsing (the Front-Office cognition).
Free-form, accent-insensitive Greek/English dialogue -> a strict SemanticFrame
(action, object, location, timestamp, constraints, confidence). Operates ONLY on
tokenised text (no PII). Optional live LLM path (Claude) refines the frame when
ANTHROPIC_API_KEY is set; otherwise fully deterministic and offline.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional


class Vertical(str, Enum):
    MEDICAL = "medical"
    HOTEL = "hotel"


class Action:
    BOOK = "BOOK"
    CANCEL = "CANCEL"
    RESCHEDULE = "RESCHEDULE"
    INFO = "INFO"
    UNKNOWN = "UNKNOWN"
    BOOKING = {"BOOK", "CANCEL", "RESCHEDULE"}


@dataclass
class SemanticFrame:
    action: str
    object: str
    timestamp: str
    location: Optional[str] = None
    constraints: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    nlu_source: str = "deterministic"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action, "object": self.object, "timestamp": self.timestamp,
            "location": self.location, "constraints": self.constraints,
            "confidence": self.confidence, "nlu_source": self.nlu_source,
        }


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


_SERVICE_MAP = {
    Vertical.MEDICAL: {
        "καθαρισμ": "Καθαρισμός δοντιών", "λευκανσ": "Λεύκανση", "εμφραξ": "Σφράγισμα",
        "σφραγ": "Σφράγισμα", "εξαγωγ": "Εξαγωγή", "δοντ": "Οδοντιατρική εξέταση",
        "εξετασ": "Ιατρική εξέταση", "ραντεβ": "Ιατρικό ραντεβού",
        "cleaning": "Dental cleaning", "checkup": "Check-up", "filling": "Filling",
    },
    Vertical.HOTEL: {
        "σουιτ": "Suite", "δωματ": "Κράτηση δωματίου", "διανυκτερ": "Διανυκτέρευση",
        "suite": "Suite", "room": "Room booking", "stay": "Overnight stay",
    },
}

_ACTION_CUES = [
    (Action.CANCEL, ["ακυρω", "ακυρ", "cancel"]),
    (Action.RESCHEDULE, ["αλλαγ", "αλλαξ", "μεταφορ", "reschedule", "change"]),
    (Action.BOOK, ["ραντεβ", "κρατησ", "κλεισ", "θελω", "ηθελα", "book", "reserve"]),
    (Action.INFO, ["ποσο", "τιμη", "ωρες", "price", "info", "hours"]),
]


def _extract_slot(low_na: str):
    base = datetime.now()
    day = base
    if "μεθαυριο" in low_na:
        day = base + timedelta(days=2)
    elif "αυριο" in low_na or "tomorrow" in low_na:
        day = base + timedelta(days=1)
    elif "σημερα" in low_na or "today" in low_na:
        day = base
    hour, minute, found = 10, 0, False
    m = re.search(r"(\d{1,2})[:.](\d{2})", low_na)
    if m:
        hour, minute, found = int(m.group(1)), int(m.group(2)), True
    else:
        m2 = re.search(r"(?:στις|at)\s+(\d{1,2})|(\d{1,2})\s*(?:μμ|πμ|pm|am)", low_na)
        if m2:
            hour = int(m2.group(1) or m2.group(2))
            found = True
            if re.search(r"\d\s*(?:μμ|pm)", low_na) and hour < 12:
                hour += 12
            elif "πμ" not in low_na and "am" not in low_na and hour < 9:
                hour += 12
    hour, minute = max(0, min(hour, 23)), max(0, min(minute, 59))
    slot = day.replace(hour=hour, minute=minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
    return slot, 1.0 if found else 0.0


def _extract_location(low_na: str) -> Optional[str]:
    for key, label in {"κεντρο": "Υποκατάστημα Κέντρο", "γλυφαδα": "Υποκατάστημα Γλυφάδα",
                       "ψυχικο": "Υποκατάστημα Ψυχικό"}.items():
        if key in low_na:
            return label
    return None


def _extract_constraints(low_na: str) -> Dict[str, Any]:
    c: Dict[str, Any] = {}
    m = re.search(r"(\d+)\s*(?:ατομ|persons?|people|guests?)", low_na)
    if m:
        c["party_size"] = int(m.group(1))
    if "επειγον" in low_na or "urgent" in low_na:
        c["priority"] = "urgent"
    # time-of-day preference (used by the ReAct re-planner)
    if "πρωι" in low_na or "morning" in low_na:
        c["time_pref"] = "morning"
    elif "μεσημερ" in low_na or "απογευμα" in low_na or "afternoon" in low_na:
        c["time_pref"] = "afternoon"
    elif "βραδ" in low_na or "evening" in low_na:
        c["time_pref"] = "evening"
    if "συντομ" in low_na or "επειγον" in low_na or "asap" in low_na or "urgent" in low_na:
        c["time_pref"] = "asap"  # urgency dominates
    return c


def _llm_frame(tokenized_text: str, vertical: Vertical) -> Optional[dict]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        client = anthropic.Anthropic()
        hint = ('{"action":"BOOK|CANCEL|RESCHEDULE|INFO|UNKNOWN","object":"service",'
                '"timestamp":"YYYY-MM-DDTHH:MM","location":null,"constraints":{},"confidence":0.0}')
        msg = client.messages.create(
            model="claude-opus-4-8", max_tokens=400,
            system=("Convert the message into a strict booking semantic frame. Keep any "
                    "[TOKEN_xxxx] placeholders untouched. Reply ONLY JSON: " + hint),
            messages=[{"role": "user", "content": f"vertical={vertical.value}\nmessage: {tokenized_text}"}])
        raw = "".join(getattr(b, "text", "") for b in msg.content)
        s, e = raw.find("{"), raw.rfind("}")
        return json.loads(raw[s:e + 1]) if s >= 0 else None
    except Exception as exc:
        print(f"[NLU] LLM frame skipped ({exc.__class__.__name__}); deterministic used")
        return None


def parse_frame(tokenized_text: str, vertical: Vertical) -> SemanticFrame:
    low_na = strip_accents(tokenized_text.lower())

    action = Action.UNKNOWN
    for act, cues in _ACTION_CUES:
        if any(strip_accents(c) in low_na for c in cues):
            action = act
            break

    obj = "Γενικό αίτημα"
    for key, label in _SERVICE_MAP[vertical].items():
        if strip_accents(key) in low_na:
            obj = label
            break

    slot, time_conf = _extract_slot(low_na)
    location = _extract_location(low_na)
    constraints = _extract_constraints(low_na)
    confidence = round(0.4 * (action != Action.UNKNOWN) + 0.3 * (obj != "Γενικό αίτημα") + 0.3 * time_conf, 2)
    source = "deterministic"

    llm = _llm_frame(tokenized_text, vertical)
    if llm:
        source = "llm"
        action = str(llm.get("action", action)).upper() if llm.get("action") else action
        obj = llm.get("object") or obj
        slot = llm.get("timestamp") or slot
        if llm.get("location") is not None:
            location = llm.get("location")
        if isinstance(llm.get("constraints"), dict):
            constraints = llm["constraints"]
        try:
            confidence = max(confidence, min(1.0, float(llm.get("confidence", confidence))))
        except (TypeError, ValueError):
            pass

    return SemanticFrame(action=action, object=obj, timestamp=slot, location=location,
                         constraints=constraints, confidence=confidence, nlu_source=source)
