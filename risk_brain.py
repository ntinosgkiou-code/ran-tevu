"""
risk_brain.py — εξηγήσιμο no-show risk για τα ΠΡΑΓΜΑΤΙΚΑ bookings του Ran-Tevu.
================================================================================
Ίδια φιλοσοφία με το OmniOS clinic_brain: deterministic, INTERPRETABLE — κάθε score με ΛΟΓΟΥΣ,
ώστε ο γιατρός να εμπιστεύεται το «γιατί» (όχι black-box πιθανότητα). Ελληνικά δεδομένα:
no-show 15-31% (verified intel). Ιστορικό ανά τηλέφωνο από τον πίνακα bookings.

Χρήση: κάρτα «Ραντεβού σε ρίσκο» στο back-office + στόχευση escalated reminders.
Additive & ασφαλές: ΔΙΑΒΑΖΕΙ μόνο — δεν στέλνει τίποτα μόνο του.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import database as db

_BASE_NEW = 0.35     # νέος ασθενής χωρίς ιστορικό → medium (ελλην. μ.ό. no-show 15-31%)


def _days_until(date_iso: str) -> int | None:
    try:
        return (datetime.strptime(date_iso, "%Y-%m-%d").date() - date.today()).days
    except (ValueError, TypeError):
        return None


def score_booking(slug: str, booking: dict, history: list[dict] | None = None) -> dict[str, Any]:
    """Risk 0..1 + band + ΛΟΓΟΙ για ΕΝΑ επερχόμενο booking. `history` = όλα τα παλιά bookings
    του ίδιου τηλεφώνου (περνιέται από τον caller για να μην κάνουμε N+1 queries)."""
    phone = (booking.get("phone") or "").strip()
    if history is None:
        with db._conn() as c:
            history = [dict(r) for r in c.execute(
                "SELECT status FROM bookings WHERE slug=? AND phone=? AND id!=? AND date < ?",
                (slug, phone, booking.get("id", -1), booking.get("date", ""))).fetchall()]
    past = [h for h in history if h.get("status") in ("completed", "no_show", "cancelled")]
    no_shows = sum(1 for h in past if h["status"] == "no_show")
    cancels = sum(1 for h in past if h["status"] == "cancelled")
    total = len(past)

    risk, reasons = 0.0, []
    if total == 0:
        risk += _BASE_NEW
        reasons.append("νέος ασθενής (χωρίς ιστορικό)")
    else:
        rate = no_shows / total
        if rate > 0:
            risk += min(0.7, rate * 0.55 + 0.28)
            reasons.append(f"ιστορικό no-show {no_shows}/{total}")
        else:
            risk += 0.05
            reasons.append(f"συνεπής ({total} χωρίς no-show)")
    days = _days_until(booking.get("date", ""))
    if days is not None and days > 14:
        risk += 0.15
        reasons.append(f"κλείστηκε {days} μέρες νωρίς")
    elif days is not None and days <= 1:
        risk -= 0.05
        reasons.append("επικείμενο")
    if cancels >= 2:
        risk += 0.10
        reasons.append(f"{cancels} προηγούμενες ακυρώσεις")
    if not phone:
        risk += 0.10
        reasons.append("χωρίς τηλέφωνο (δεν φτάνει υπενθύμιση)")

    risk = round(max(0.0, min(1.0, risk)), 2)
    band = "high" if risk >= 0.6 else "medium" if risk >= 0.33 else "low"
    return {"booking_id": booking.get("id"), "name": booking.get("name", ""),
            "date": booking.get("date"), "time": booking.get("time"),
            "risk": risk, "band": band, "reasons": reasons}


def recall_candidates(slug: str, min_days: int = 60, limit: int = 20) -> dict[str, Any]:
    """RECALL ENGINE: «ποιος ασθενής έπρεπε να έχει ξανάρθει» — το χαμένο revenue που κανείς δεν
    κυνηγάει. Ασθενείς με τελευταία ολοκληρωμένη επίσκεψη ≥min_days πριν, ΧΩΡΙΣ επερχόμενο ραντεβού,
    ταξινομημένοι κατά € δυναμικό (ιστορικός μέσος όρος ΤΟΥΣ, όχι υπόθεση). Με τον λόγο, πάντα."""
    today = date.today().strftime("%Y-%m-%d")
    with _conn_ro() as c:
        rows = [dict(r) for r in c.execute("""
            SELECT b.phone, MAX(b.name) name, COUNT(*) visits, MAX(b.date) last_visit,
                   (SELECT COUNT(*) FROM bookings f WHERE f.slug=b.slug AND f.phone=b.phone
                    AND f.date>=? AND f.status='confirmed') upcoming,
                   COALESCE((SELECT AVG(i.total) FROM bo_invoices i
                             WHERE i.slug=b.slug AND i.patient_phone=b.phone AND i.total>0), 0) avg_eur
            FROM bookings b
            WHERE b.slug=? AND b.status='completed' AND b.phone!=''
            GROUP BY b.phone""", (today, slug)).fetchall()]
    out = []
    for r in rows:
        if r["upcoming"] > 0:
            continue                                    # έχει ήδη ραντεβού — δεν είναι «χαμένος»
        try:
            days_gone = (date.today() - datetime.strptime(r["last_visit"], "%Y-%m-%d").date()).days
        except (ValueError, TypeError):
            continue
        if days_gone < min_days:
            continue
        est = round(r["avg_eur"], 2) if r["avg_eur"] else 0.0
        out.append({"name": r["name"], "phone": r["phone"], "last_visit": r["last_visit"],
                    "days_gone": days_gone, "visits": r["visits"], "est_eur": est,
                    "reason": (f"{r['visits']} επισκέψεις · τελευταία πριν {days_gone} μέρες"
                               + (f" · μ.ό. €{est:g}/επίσκεψη" if est else ""))})
    out.sort(key=lambda x: (x["est_eur"], x["days_gone"]), reverse=True)
    total = round(sum(x["est_eur"] for x in out), 2)
    return {"candidates": out[:limit], "total": len(out), "eur_potential": total}


def revenue_analytics(slug: str, days: int = 90) -> dict[str, Any]:
    """REVENUE ANALYTICS: € ανά υπηρεσία + ανά ημέρα εβδομάδας από ΠΡΑΓΜΑΤΙΚΑ τιμολόγια —
    «τι αφήνει λεφτά και πότε», ώστε ο γιατρός να αποφασίζει με νούμερα, όχι αίσθηση."""
    since = (date.today() - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%d")
    gr_days = ["Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή"]
    with _conn_ro() as c:
        by_service = [dict(r) for r in c.execute("""
            SELECT COALESCE(NULLIF(service,''),'(χωρίς υπηρεσία)') service,
                   COUNT(*) n, ROUND(SUM(total),2) eur, ROUND(SUM(paid),2) paid_eur
            FROM bo_invoices WHERE slug=? AND issue_date>=?
            GROUP BY 1 ORDER BY eur DESC""", (slug, since)).fetchall()]
        dow_rows = [dict(r) for r in c.execute("""
            SELECT CAST(strftime('%w', issue_date) AS INTEGER) dow,
                   COUNT(*) n, ROUND(SUM(total),2) eur
            FROM bo_invoices WHERE slug=? AND issue_date>=? GROUP BY 1""", (slug, since)).fetchall()]
    by_dow = [{"weekday": gr_days[(r["dow"] + 6) % 7], "n": r["n"], "eur": r["eur"]}
              for r in sorted(dow_rows, key=lambda x: x["eur"] or 0, reverse=True)]
    total = round(sum(r["eur"] or 0 for r in by_service), 2)
    unpaid = round(sum((r["eur"] or 0) - (r["paid_eur"] or 0) for r in by_service), 2)
    top = by_service[0] if by_service else None
    return {"window_days": days, "total_eur": total, "unpaid_eur": unpaid,
            "by_service": by_service, "by_weekday": by_dow,
            "insight": (f"Σύνολο €{total:g} σε {days} μέρες. Κορυφαία υπηρεσία: {top['service']} "
                        f"(€{top['eur']:g}). Ανείσπρακτα: €{unpaid:g}." if top
                        else "Δεν υπάρχουν τιμολόγια στο διάστημα — τα analytics ανάβουν με τα πρώτα.")}


def _conn_ro():
    return db._conn()


def scan_upcoming(slug: str, horizon_days: int = 7) -> dict[str, Any]:
    """Σκανάρει τα επερχόμενα confirmed bookings του ιατρείου και τα ταξινομεί κατά ρίσκο.
    Ένα query για ΟΛΟ το ιστορικό (όχι N+1) — γρήγορο και σε γεμάτο ημερολόγιο."""
    today = date.today().strftime("%Y-%m-%d")
    with db._conn() as c:
        upcoming = [dict(r) for r in c.execute(
            "SELECT id, name, phone, date, time, service, status FROM bookings "
            "WHERE slug=? AND status='confirmed' AND date>=? ORDER BY date, time",
            (slug, today)).fetchall()]
        hist_rows = [dict(r) for r in c.execute(
            "SELECT phone, status, date FROM bookings WHERE slug=? AND date<?",
            (slug, today)).fetchall()]
    by_phone: dict[str, list[dict]] = {}
    for h in hist_rows:
        by_phone.setdefault((h.get("phone") or "").strip(), []).append(h)

    scored = []
    for b in upcoming:
        d = _days_until(b["date"])
        if d is not None and d <= horizon_days:
            scored.append(score_booking(slug, b, by_phone.get((b.get("phone") or "").strip(), [])))
    scored.sort(key=lambda s: s["risk"], reverse=True)
    return {"scanned": len(scored),
            "high": [s for s in scored if s["band"] == "high"],
            "medium": [s for s in scored if s["band"] == "medium"],
            "appointments": scored}
