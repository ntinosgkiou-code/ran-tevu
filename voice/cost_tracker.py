"""
Per-call cost tracking + customer billing.

Vapi μας στέλνει cost στο end-of-call-report. Εμείς:
- Σώζουμε σε voice_calls.cost_usd
- Aggregate ανά business για μηνιαία τιμολόγηση
- Alert αν customer ξεπεράσει quota

Pricing model (σε εμάς):
- Vapi:        $0.05/min (orchestration + STT Deepgram)
- ElevenLabs:  ~$0.18/min ($22/μήνα 100K chars ÷ ~120 min)
- Claude:      ~$0.01/min (low usage with maxTokens=250)
- Twilio:      $0.013/min inbound + $1/μήνα number
- TOTAL:       ~$0.25/min ≈ €0.23/min

Pricing model (σε πελάτη):
- €49/μήνα flat: αρκεί για 60 min/μήνα = €0.82/min, margin 75%
- €99/μήνα flat: αρκεί για 200 min/μήνα = €0.50/min, margin 55%
- €3/booking: pay-per-success, no cap (μεγαλύτερο potential)
"""
from datetime import date, datetime
import database as db


def monthly_cost_for_business(slug: str, year: int = None,
                              month: int = None) -> dict:
    """Συνολικό cost για ένα business σε έναν μήνα."""
    if year is None:
        year = date.today().year
    if month is None:
        month = date.today().month

    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year+1:04d}-01-01"
    else:
        end = f"{year:04d}-{month+1:02d}-01"

    with db._conn() as c:
        row = c.execute("""
            SELECT
                COUNT(*) AS calls_count,
                COALESCE(SUM(duration_seconds), 0) AS total_seconds,
                COALESCE(SUM(cost_usd), 0) AS vapi_cost_usd
            FROM voice_calls
            WHERE business_slug=?
              AND started_at >= ? AND started_at < ?
        """, (slug, start, end)).fetchone()

        bookings_row = c.execute("""
            SELECT COUNT(*) AS bookings_count
            FROM bookings
            WHERE slug=?
              AND created >= ? AND created < ?
              AND notes LIKE '[VOICE]%'
        """, (slug, start, end)).fetchone()

    calls = int(row["calls_count"]) if row else 0
    total_sec = int(row["total_seconds"]) if row else 0
    total_min = total_sec / 60.0
    vapi_cost = float(row["vapi_cost_usd"]) if row else 0.0
    bookings = int(bookings_row["bookings_count"]) if bookings_row else 0

    # Our cost estimate
    our_cost_eur = total_min * 0.23

    return {
        "business_slug": slug,
        "year": year,
        "month": month,
        "calls": calls,
        "total_minutes": round(total_min, 2),
        "voice_bookings": bookings,
        "vapi_reported_cost_usd": round(vapi_cost, 4),
        "estimated_our_cost_eur": round(our_cost_eur, 2),
        "billable_flat_eur_49": 49,
        "billable_flat_eur_99": 99,
        "billable_per_booking_eur": bookings * 3,
        "margin_at_99_flat_eur": round(99 - our_cost_eur, 2),
    }


def check_quota_alert(slug: str, monthly_minute_quota: int = 60) -> dict:
    """Επιστρέφει alert αν business πλησιάζει quota."""
    stats = monthly_cost_for_business(slug)
    used_pct = (stats["total_minutes"] / monthly_minute_quota) * 100
    return {
        "slug": slug,
        "used_minutes": stats["total_minutes"],
        "quota_minutes": monthly_minute_quota,
        "used_pct": round(used_pct, 1),
        "alert_level": (
            "critical" if used_pct >= 100 else
            "warning"  if used_pct >= 80 else
            "ok"
        ),
    }


def all_businesses_monthly_summary() -> list:
    """Για admin dashboard — όλα τα voice agents + monthly stats."""
    with db._conn() as c:
        rows = c.execute("""
            SELECT business_slug FROM voice_agents WHERE status='active'
        """).fetchall()

    return [monthly_cost_for_business(r["business_slug"]) for r in rows]
