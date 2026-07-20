"""
clinic_brain.py — εξηγήσιμο no-show risk + αυτόνομη ενέργεια «η λίστα γεμίζει μόνη της».
=======================================================================================
Deterministic, INTERPRETABLE μοντέλο (όχι black-box): κάθε score συνοδεύεται από ΤΟΥΣ ΛΟΓΟΥΣ,
ώστε ο γιατρός να εμπιστεύεται το «γιατί». Χτισμένο στα ελληνικά δεδομένα (no-show 15-31%,
πηγή: rantevu_deep_intel). Feeds τον autopilot: υψηλό ρίσκο → escalated reminder + pre-offer του
slot στη waitlist (overbook-insurance), ώστε η ακύρωση να μη γίνεται ποτέ κενό.

Γιατί deterministic κι όχι ML: σε κλινικό setting το «γιατί» μετράει περισσότερο από ένα .01 AUC.
Ο γιατρός βλέπει «flag γιατί 2/3 no-show + οφειλή €40», όχι μια αδιαφανή πιθανότητα.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from clinic_store import today_iso

_GR_WEEKDAY = ["Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή"]
_PAST = ("completed", "no_show", "cancelled")
_UPCOMING = ("booked", "confirmed")
_BASE_NEW = 0.35          # baseline νέου ασθενή (ελληνικός μ.ό. no-show ~15-31% → medium band)


def _days_until(date_iso: str) -> int | None:
    try:
        return (_dt.date.fromisoformat(date_iso) - _dt.date.fromisoformat(today_iso())).days
    except (ValueError, TypeError):
        return None


def score_appointment(store, appt: dict) -> dict[str, Any]:
    """Επιστρέφει risk 0..1 + band + ΛΟΓΟΥΣ + προτεινόμενη ενέργεια για ΕΝΑ ραντεβού."""
    hist = store.patient_history(appt["patient"])
    past = [a for a in hist["appointments"] if a.get("status") in _PAST]
    no_shows = sum(1 for a in past if a["status"] == "no_show")
    cancels = sum(1 for a in past if a["status"] == "cancelled")
    total = len(past)
    balance = hist.get("balance", 0.0)

    risk = 0.0
    reasons: list[str] = []

    if total == 0:
        risk += _BASE_NEW
        reasons.append("νέος ασθενής (χωρίς ιστορικό)")
    else:
        ns_rate = no_shows / total
        if ns_rate > 0:
            risk += min(0.7, ns_rate * 0.55 + 0.28)
            reasons.append(f"ιστορικό no-show {no_shows}/{total}")
        else:
            risk += 0.05
            reasons.append(f"συνεπής ({total} χωρίς no-show)")

    days = _days_until(appt.get("date", ""))
    if days is not None and days > 14:
        risk += 0.15
        reasons.append(f"κλείστηκε {days} μέρες νωρίς (μακρύ lead time)")
    elif days is not None and days <= 1:
        risk -= 0.05
        reasons.append("επικείμενο (χαμηλότερο ρίσκο)")

    if balance and balance > 0:
        risk += 0.10
        reasons.append(f"εκκρεμεί οφειλή €{balance:g}")
    if cancels >= 2:
        risk += 0.10
        reasons.append(f"{cancels} προηγούμενες ακυρώσεις")

    risk = round(max(0.0, min(1.0, risk)), 2)
    band = "high" if risk >= 0.6 else "medium" if risk >= 0.33 else "low"

    if band == "high":
        action = "escalated_reminder+waitlist_preoffer"
        action_gr = "Έξτρα υπενθύμιση (κλήση/SMS) + προσφορά του slot στη λίστα αναμονής ως ασφάλεια."
    elif band == "medium":
        action = "extra_reminder"
        action_gr = "Δεύτερη υπενθύμιση 24ω + 2ω πριν."
    else:
        action = "standard_reminder"
        action_gr = "Κανονική υπενθύμιση."

    return {
        "appointment_id": appt.get("id"), "patient": appt["patient"],
        "date": appt.get("date"), "time": appt.get("time"),
        "risk": risk, "band": band, "reasons": reasons,
        "action": action, "action_gr": action_gr,
    }


def scan_clinic(store, horizon_days: int = 14) -> dict[str, Any]:
    """Σκανάρει ΟΛΑ τα επερχόμενα ραντεβού, τα σκοράρει, και βγάζει actions ταξινομημένα κατά ρίσκο.
    Αυτό τρέχει ο autopilot: ο γιατρός δεν κάνει τίποτα — η λίστα διαχειρίζεται μόνη της."""
    today = today_iso()
    scored = []
    for a in store.list_appointments():
        if a.get("status") in _UPCOMING and a.get("date", "") >= today:
            d = _days_until(a["date"])
            if d is not None and d <= horizon_days:
                scored.append(score_appointment(store, a))
    scored.sort(key=lambda s: s["risk"], reverse=True)
    high = [s for s in scored if s["band"] == "high"]
    return {
        "scanned": len(scored),
        "at_risk": len([s for s in scored if s["band"] != "low"]),
        "high_risk": len(high),
        "eur_at_risk": round(sum(store.get_clinic().default_value for _ in high), 2),
        "appointments": scored,
    }
