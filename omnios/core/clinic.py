"""
clinic.py — the clinic's grounded knowledge base + ops policy.
Single source of truth for FAQ answers and autonomous-task timing. In a real
deployment this is loaded per-tenant; here it's a sensible default. Grounded
answers only — the agent never invents hours/prices (liability).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class ClinicProfile:
    name: str = "Κεντρικό Ιατρείο"
    hours: str = "Δευτέρα–Παρασκευή 09:00–21:00, Σάββατο 09:00–14:00"
    location: str = "Λ. Κηφισίας 1, Αθήνα"
    insurance: str = "ΕΟΠΥΥ + ιδιωτικά συμβόλαια"
    services: Dict[str, int] = field(default_factory=lambda: {
        "Καθαρισμός δοντιών": 50, "Σφράγισμα": 60, "Εξαγωγή": 80,
        "Λεύκανση": 250, "Ιατρική εξέταση": 40, "Οδοντιατρική εξέταση": 30,
    })
    reminder_lead_hours: int = 48
    confirm_lead_hours: int = 72
    recall_days: int = 180


CLINIC = ClinicProfile()


def faq_answer(text: str, frame: Any) -> str:
    """Grounded answer to an INFO request — hours / prices / location / insurance."""
    low = (text or "").lower()
    obj = getattr(frame, "object", "") or ""

    if any(k in low for k in ["ποσο", "τιμη", "κοστ", "price", "cost"]):
        if obj in CLINIC.services:
            return f"Η τιμή για {obj} είναι €{CLINIC.services[obj]}."
        for service, price in CLINIC.services.items():
            if service.lower() in low:
                return f"Η τιμή για {service} είναι €{price}."
        return "Τιμοκατάλογος: " + ", ".join(f"{s} €{p}" for s, p in CLINIC.services.items()) + "."
    if any(k in low for k in ["ωρες", "ωραριο", "hours", "open", "ανοιχτ"]):
        return f"Ωράριο: {CLINIC.hours}."
    if any(k in low for k in ["που ", "διευθυνση", "location", "address", "βρισκ"]):
        return f"Διεύθυνση: {CLINIC.location}."
    if any(k in low for k in ["ασφαλ", "εοπυυ", "insurance", "ταμει"]):
        return f"Ασφάλιση: {CLINIC.insurance}."
    return (f"Πληροφορίες — ωράριο {CLINIC.hours}· {CLINIC.location}· {CLINIC.insurance}.")
