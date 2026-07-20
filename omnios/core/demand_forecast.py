"""
demand_forecast.py — πρόβλεψη ζήτησης & εσόδων πάνω στο ΠΡΑΓΜΑΤΙΚΟ clinic store.
================================================================================
Ίδια dual-mode φιλοσοφία με το προϊόν: Google TimesFM (zero-shot, CPU) όταν είναι εγκατεστημένο,
τίμιο per-weekday στατιστικό fallback αλλιώς — ίδιο σχήμα εξόδου, ρητό `engine` attribution,
και honest ready:false κάτω από 14 μέρες ιστορικού (δεν εφευρίσκουμε προβλέψεις).
"""
from __future__ import annotations

import datetime as _dt
from collections import defaultdict
from typing import Any, Optional

_MIN_HISTORY_DAYS = 14
_ACTIVE = ("confirmed", "booked", "completed", "checked_in", "no_show")
_GR = ["Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή"]


def _daily_series(store) -> tuple[list[str], list[float], float]:
    """(dates, counts, μέση αξία συνεδρίας) από τα ραντεβού του tenant, με κενά=0."""
    appts = [a for a in store.list_appointments() if a.get("status") in _ACTIVE]
    if not appts:
        return [], [], 0.0
    by_date: dict[str, int] = defaultdict(int)
    vals: list[float] = []
    for a in appts:
        by_date[a["date"]] += 1
        vals.append(float(a.get("value", 0)))
    try:
        start = _dt.date.fromisoformat(min(by_date))
        end = _dt.date.fromisoformat(max(by_date))
    except ValueError:
        return [], [], 0.0
    dates, counts = [], []
    d = start
    while d <= end:
        iso = d.isoformat()
        dates.append(iso)
        counts.append(float(by_date.get(iso, 0)))
        d += _dt.timedelta(days=1)
    avg_value = (sum(vals) / len(vals)) if vals else 0.0
    return dates, counts, avg_value


def _timesfm(counts: list[float], horizon: int) -> Optional[list[float]]:
    try:
        import timesfm  # optional — ΔΕΝ είναι στα requirements (1.5GB RAM, βαρύ image)
    except Exception:
        return None
    try:
        tfm = timesfm.TimesFm(
            hparams=timesfm.TimesFmHparams(backend="cpu", horizon_len=horizon),
            checkpoint=timesfm.TimesFmCheckpoint(huggingface_repo_id="google/timesfm-2.0-500m-pytorch"),
        )
        point, _ = tfm.forecast([counts], freq=[0])
        return [max(0.0, float(x)) for x in point[0][:horizon]]
    except Exception:
        return None


def _seasonal(dates: list[str], counts: list[float], horizon: int) -> list[float]:
    dow: dict[int, list[float]] = defaultdict(list)
    for iso, n in zip(dates, counts):
        dow[_dt.date.fromisoformat(iso).weekday()].append(n)
    dow_avg = {k: sum(v) / len(v) for k, v in dow.items()}
    overall = sum(counts) / len(counts) if counts else 0.0
    half = len(counts) // 2 or 1
    trend = ((sum(counts[half:]) / max(1, len(counts) - half)) -
             (sum(counts[:half]) / half)) / max(1, horizon)
    last = _dt.date.fromisoformat(dates[-1])
    return [round(max(0.0, dow_avg.get((last + _dt.timedelta(days=i)).weekday(), overall)
                      + trend * i), 1) for i in range(1, horizon + 1)]


def forecast_clinic(store, horizon: int = 14) -> dict[str, Any]:
    """Πρόβλεψη ζήτησης + αναμενόμενων εσόδων για τον tenant του store."""
    dates, counts, avg_value = _daily_series(store)
    if len(dates) < _MIN_HISTORY_DAYS:
        return {"ready": False, "history_days": len(dates),
                "reason": f"Χρειάζονται ≥{_MIN_HISTORY_DAYS} μέρες ιστορικού (υπάρχουν {len(dates)})."}
    horizon = max(1, min(int(horizon), 30))
    preds = _timesfm(counts, horizon)
    engine = "google-timesfm" if preds is not None else "seasonal-statistical"
    if preds is None:
        preds = _seasonal(dates, counts, horizon)
    last = _dt.date.fromisoformat(dates[-1])
    days = []
    for i, v in enumerate(preds, start=1):
        d = last + _dt.timedelta(days=i)
        days.append({"date": d.isoformat(), "weekday": _GR[d.weekday()],
                     "expected_bookings": round(v, 1),
                     "expected_eur": round(v * avg_value, 0)})
    busiest = max(days, key=lambda p: p["expected_bookings"])
    quietest = min(days, key=lambda p: p["expected_bookings"])
    total_eur = round(sum(p["expected_eur"] for p in days), 0)
    return {"ready": True, "engine": engine, "horizon": horizon, "history_days": len(dates),
            "avg_session_eur": round(avg_value, 2), "expected_total_eur": total_eur,
            "busiest": busiest, "quietest": quietest, "days": days,
            "insight": (f"Αναμενόμενα έσοδα {horizon} ημερών ≈ €{total_eur:g}. "
                        f"Πιο γεμάτη: {busiest['weekday']} ({busiest['expected_bookings']}) · "
                        f"πιο ήσυχη: {quietest['weekday']} — ιδανική για recall.")}
