"""
Unified Admin Dashboard για κάθε business.

Σε ΕΝΑ page:
- Today's bookings (από bookings table)
- Recent voice calls (από voice_calls)
- Pipeline stage breakdown (από outreach_leads ή pipeline table)
- Revenue / cost summary (από bookings + voice cost_tracker)
- Quick actions (νέο ραντεβού, στείλε campaign, αγόρασε number)

Sidebar navigation: Dashboard, Calendar, Voice, Chat, Leads, Campaigns, Pipeline, Settings.
"""
from flask import Blueprint, request, jsonify, send_from_directory, abort, render_template_string
from functools import wraps
import os
import json
from datetime import date, datetime, timedelta
import database as db

bp = Blueprint("dashboard", __name__)


def require_master(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        token = (
            request.headers.get("Authorization", "").replace("Bearer ", "").strip()
            or request.args.get("token", "").strip()
        )
        expected = os.environ.get("ADMIN_TOKEN", "")
        if not token or token != expected:
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrap


# ── Dashboard data feed ─────────────────────────────────────────────────

@bp.get("/api/admin/<slug>/dashboard-feed")
@require_master
def dashboard_feed(slug):
    """All data needed by the dashboard in one call.
    Returns: today bookings, recent voice calls, pipeline stats, revenue."""
    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    month_start = date.today().replace(day=1).isoformat()

    business = db.get_business(slug)
    if not business:
        return jsonify({"error": "business not found"}), 404

    with db._conn() as c:
        # ── Today's bookings ─────────────
        today_bookings = c.execute("""
            SELECT id, time, service, name, phone, status, duration
            FROM bookings
            WHERE slug=? AND date=?
            ORDER BY time
        """, (slug, today)).fetchall()

        # ── This week stats ──────────────
        week_stats = c.execute("""
            SELECT
                COUNT(*) as total,
                COALESCE(SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END), 0) as confirmed,
                COALESCE(SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END), 0) as cancelled
            FROM bookings
            WHERE slug=? AND date >= ?
        """, (slug, week_ago)).fetchone()

        # ── Month bookings revenue ───────
        month_bookings_rows = c.execute("""
            SELECT service, duration FROM bookings
            WHERE slug=? AND date >= ? AND status != 'cancelled'
        """, (slug, month_start)).fetchall()

        # ── Recent voice calls (if voice_calls table exists) ───
        voice_calls = []
        try:
            voice_calls = [dict(r) for r in c.execute("""
                SELECT id, vapi_call_id, from_number, started_at,
                       duration_seconds, cost_usd, summary, success_evaluation
                FROM voice_calls
                WHERE business_slug=?
                ORDER BY started_at DESC LIMIT 10
            """, (slug,)).fetchall()]
            from crypto_util import decrypt as _dec
            for _vc in voice_calls:
                if _vc.get("summary"):
                    _vc["summary"] = _dec(_vc["summary"])
        except Exception:
            pass

        # ── Voice cost this month ────────
        voice_cost = {"calls": 0, "minutes": 0, "cost_usd": 0.0}
        try:
            vc_row = c.execute("""
                SELECT COUNT(*) as calls,
                       COALESCE(SUM(duration_seconds),0)/60.0 as minutes,
                       COALESCE(SUM(cost_usd),0) as cost_usd
                FROM voice_calls
                WHERE business_slug=? AND started_at >= ?
            """, (slug, month_start)).fetchone()
            voice_cost = dict(vc_row) if vc_row else voice_cost
        except Exception:
            pass

        # ── Outreach pipeline stats (if exists) ──
        pipeline_stats = {"new": 0, "contacted": 0, "replied": 0, "won": 0}
        try:
            for status in pipeline_stats.keys():
                r = c.execute("""
                    SELECT COUNT(*) AS n FROM outreach_leads WHERE status=?
                """, (status,)).fetchone()
                pipeline_stats[status] = int(r["n"]) if r else 0
        except Exception:
            pass

        # ── Waitlist count ───────────────
        waitlist_count = 0
        try:
            r = c.execute("SELECT COUNT(*) as n FROM waitlist WHERE slug=?", (slug,)).fetchone()
            waitlist_count = int(r["n"]) if r else 0
        except Exception:
            pass

        # ── Client count ─────────────────
        client_count = 0
        try:
            r = c.execute("SELECT COUNT(DISTINCT phone) as n FROM client_profiles WHERE slug=?", (slug,)).fetchone()
            client_count = int(r["n"]) if r else 0
        except Exception:
            pass

    # Estimate revenue
    services = business.get("services", "[]")
    if isinstance(services, str):
        try:
            services = json.loads(services)
        except Exception:
            services = []
    price_map = {(s.get("name") or "").lower(): float(s.get("price") or 0) for s in services if isinstance(s, dict)}
    month_revenue = sum(price_map.get((b["service"] or "").lower(), 0) for b in month_bookings_rows)

    return jsonify({
        "business": {
            "slug": slug,
            "name": business.get("name", ""),
            "type": business.get("type", ""),
        },
        "today": {
            "date": today,
            "bookings": [dict(b) for b in today_bookings],
            "count": len(today_bookings),
        },
        "week": {
            "total_bookings": int(week_stats["total"] or 0) if week_stats else 0,
            "confirmed": int(week_stats["confirmed"] or 0) if week_stats else 0,
            "cancelled": int(week_stats["cancelled"] or 0) if week_stats else 0,
        },
        "month": {
            "bookings": len(month_bookings_rows),
            "revenue_eur": round(month_revenue, 2),
        },
        "voice": {
            "recent_calls": voice_calls,
            "monthly": {
                "calls": int(voice_cost.get("calls", 0)),
                "minutes": round(float(voice_cost.get("minutes", 0)), 1),
                "cost_usd": round(float(voice_cost.get("cost_usd", 0)), 2),
            },
        },
        "pipeline": pipeline_stats,
        "waitlist_count": waitlist_count,
        "client_count": client_count,
    })


# ── Proof-of-value ROI (retention όπλο + sales asset) ──────────────────

@bp.get("/api/admin/<slug>/roi")
@require_master
def roi_feed(slug):
    """JSON: τι αξία έδωσε το AI (€ saved, calls, bookings, recovered no-shows)."""
    if not db.get_business(slug):
        return jsonify({"error": "business not found"}), 404
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        days = 30
    return jsonify(db.roi_summary(slug, days=max(1, min(days, 365))))


@bp.get("/<slug>/roi")
def roi_page(slug):
    """Shareable proof-of-value σελίδα (token-gated). Το screenshot → case study/content."""
    business = db.get_business(slug)
    if not business:
        abort(404)
    token = request.args.get("t", "")
    if token != os.environ.get("ADMIN_TOKEN", ""):
        return "Μη εξουσιοδοτημένη πρόσβαση", 403
    r = db.roi_summary(slug, days=30)
    name = business.get("name", "")
    html = f"""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Η αξία του AI — {name}</title></head>
<body style="margin:0;background:#f6f8fb;font-family:-apple-system,Segoe UI,Roboto,Inter,sans-serif;color:#0f172a;">
<div style="max-width:520px;margin:0 auto;padding:48px 24px;text-align:center;">
  <p style="font-size:13px;letter-spacing:2px;text-transform:uppercase;color:#94a3b8;margin:0;">Ran-Tevu · {name}</p>
  <p style="font-size:15px;color:#475569;margin:8px 0 20px;">Τι σου έκανε ο AI βοηθός τις τελευταίες 30 μέρες</p>
  <div style="background:linear-gradient(135deg,#ecfdf5,#d1fae5);border:1px solid #a7f3d0;border-radius:16px;padding:28px 20px;box-shadow:0 1px 2px rgba(16,24,40,.04),0 4px 12px rgba(16,24,40,.05);">
    <div style="font-size:56px;font-weight:800;color:#047857;margin:0;">€{r['euros_saved']:.0f}</div>
    <p style="font-size:14px;color:#065f46;margin:6px 0 0;">αξία ραντεβού που έκλεισε για σένα</p>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:18px;">
    {_stat("📞", r['answered_calls'], "κλήσεις απαντημένες 24/7")}
    {_stat("📅", r['ai_bookings'], "ραντεβού έκλεισε το AI")}
    {_stat("🔁", r['noshow_recovery_sent'], "no-shows κυνηγήθηκαν")}
    {_stat("🔔", r['reminders_sent'], "υπενθυμίσεις στάλθηκαν")}
  </div>
  <p style="font-size:12px;color:#94a3b8;margin:36px 0 0;">Μέση αξία συνεδρίας: €{r['avg_session_value']:.0f} · συντηρητικός υπολογισμός</p>
</div></body></html>"""
    return html


def _stat(icon, value, label):
    return (f'<div style="background:#ffffff;border:1px solid #e8edf5;border-radius:16px;padding:20px 12px;box-shadow:0 1px 2px rgba(16,24,40,.04),0 4px 12px rgba(16,24,40,.05);">'
            f'<div style="font-size:22px;">{icon}</div>'
            f'<div style="font-size:30px;font-weight:700;color:#0f172a;margin:4px 0;">{value}</div>'
            f'<div style="font-size:12px;color:#64748b;line-height:1.3;">{label}</div></div>')


# ── Unified Dashboard UI ────────────────────────────────────────────────

@bp.get("/admin/<slug>/dashboard")
def dashboard_ui(slug):
    """The new unified admin dashboard."""
    business = db.get_business(slug)
    if not business:
        abort(404)
    public_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public")
    return send_from_directory(public_dir, "admin_dashboard_v2.html")


# ── Customer-facing LIVE €-dashboard (Chart.js, HMAC-gated ανά slug) ────────
import hmac as _hmac
import hashlib as _hashlib


def _dash_secret() -> bytes:
    # ΣΤΑΘΕΡΟ secret για τα customer links. ΟΧΙ ADMIN_TOKEN πρώτο — αν τον κάνεις rotate,
    # ΟΛΑ τα emailed links (dashboard/back-office) θα έσπαγαν («έληξε/μη έγκυρο»).
    # Προτίμησε σταθερό DASH_LINK_SECRET/SECRET_KEY ώστε η περιστροφή του admin token να ΜΗΝ τα πειράζει.
    s = (os.environ.get("DASH_LINK_SECRET") or os.environ.get("SECRET_KEY")
         or os.environ.get("ADMIN_TOKEN"))
    if not s:
        # FAIL-CLOSED: the hardcoded dev value is in the public repo — using it in production would
        # make every /<slug>/dashboard?t= and /back-office?t= token forgeable by anyone. Refuse.
        if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
            raise RuntimeError("Set DASH_LINK_SECRET (or SECRET_KEY/ADMIN_TOKEN) in production — "
                               "refusing to sign customer links with the insecure dev secret.")
        s = "ran-tevu-dev-only-insecure"
    return s.encode()


def dash_token(slug: str) -> str:
    """Μόνιμο HMAC token ανά business. Ο γιατρός παίρνει link
    /<slug>/dashboard?t=<token> ΧΩΡΙΣ να εκτεθεί ποτέ το ADMIN_TOKEN."""
    return _hmac.new(_dash_secret(), f"dash:{slug}".encode(), _hashlib.sha256).hexdigest()[:24]


def _check_dash_token(slug: str) -> bool:
    """HMAC ?t= token (πελάτης/γιατρός) Ή owner cookie (Ντίνος μπαίνει παντού — και όταν το
    back-office τον έστειλε εδώ μέσω iframe χωρίς ?t= στο URL, π.χ. https://.../<slug>/back-office
    ανοιγμένο μόνο με owner login — βλ. back_office_routes.py._check_token για το ίδιο μοτίβο).
    ΣΚΟΠΙΜΑ χωρίς bypass για το δημόσιο demo slug: τα οικονομικά δεν είναι δημόσια σε κανέναν."""
    try:
        from routes.start_routes import is_owner
        if is_owner():
            return True
    except Exception:
        pass
    return _hmac.compare_digest(request.args.get("t", ""), dash_token(slug))


@bp.get("/api/<slug>/value-feed")
def value_feed(slug):
    """Customer-token-gated JSON για το live dashboard: roi_summary 30d + 6μηνο timeseries."""
    if not _check_dash_token(slug):
        return jsonify({"error": "unauthorized"}), 403
    if not db.get_business(slug):
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "summary":    db.roi_summary(slug, days=30),
        "timeseries": db.roi_timeseries(slug, months=6),
    })


@bp.get("/admin/<slug>/dashboard-link")
@require_master
def dashboard_link(slug):
    """Admin-only: δίνει στον Ντίνο το shareable customer link να στείλει στον γιατρό."""
    if not db.get_business(slug):
        return jsonify({"error": "not found"}), 404
    base = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") and f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}") \
        or os.environ.get("BASE_URL", "").rstrip("/") or "https://www.ran-tevu.gr"
    return jsonify({"dashboard_url": f"{base}/{slug}/dashboard?t={dash_token(slug)}"})


_DASHBOARD_HTML = r"""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Η αξία του Ran-Tevu — __NAME__</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f6f8fb;font-family:-apple-system,'Segoe UI',Roboto,Inter,sans-serif;color:#0f172a;padding:32px 16px}
.wrap{max-width:560px;margin:0 auto}
.brand{font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#94a3b8;text-align:center}
.sub{font-size:14px;color:#475569;text-align:center;margin:6px 0 24px}
.hero{background:linear-gradient(135deg,#ecfdf5,#d1fae5);border:1px solid #a7f3d0;border-radius:16px;padding:32px 20px;text-align:center;margin-bottom:18px;box-shadow:0 1px 2px rgba(16,24,40,.04),0 4px 12px rgba(16,24,40,.05)}
.hero .big{font-size:52px;font-weight:800;color:#047857;line-height:1}
.hero .lbl{font-size:13px;color:#065f46;margin-top:8px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:18px}
.stat{background:#ffffff;border:1px solid #e8edf5;border-radius:16px;padding:18px 12px;text-align:center;box-shadow:0 1px 2px rgba(16,24,40,.04),0 4px 12px rgba(16,24,40,.05)}
.stat .v{font-size:26px;font-weight:700;color:#0f172a}
.stat .l{font-size:11px;color:#64748b;line-height:1.3;margin-top:4px}
.card{background:#ffffff;border:1px solid #e8edf5;border-radius:16px;padding:18px;margin-bottom:16px;box-shadow:0 1px 2px rgba(16,24,40,.04),0 4px 12px rgba(16,24,40,.05)}
.card h3{font-size:13px;color:#475569;margin-bottom:12px;font-weight:600}
.foot{font-size:11px;color:#94a3b8;text-align:center;margin-top:8px}
.err{text-align:center;padding:60px 20px;color:#94a3b8}
</style></head>
<body><div class="wrap">
<div class="brand">Ran-Tevu · __NAME__</div>
<div class="sub">Πόσα σου γυρίζει πίσω ο AI βοηθός σου</div>
<div id="content"><div class="err">Φόρτωση…</div></div>
<div class="foot">Συντηρητικός υπολογισμός · μέση αξία συνεδρίας βάσει των υπηρεσιών σου</div>
</div>
<script>
const SLUG="__SLUG__", TOK="__TOKEN__";
fetch(`/api/${SLUG}/value-feed?t=${TOK}`).then(r=>r.json()).then(d=>{
  if(d.error){document.getElementById('content').innerHTML='<div class="err">Μη διαθέσιμο</div>';return;}
  const s=d.summary, ts=d.timeseries;
  const months=ts.months.map(m=>m.month.slice(5));
  document.getElementById('content').innerHTML=`
    <div class="hero"><div class="big">€${Math.round(s.recovered_eur||0)}</div>
      <div class="lbl">σου ανέκτησε ο Ran-Tevu (${s.recovered_count||0} ασθενείς ξαναέκλεισαν μετά από recovery/win-back, ${s.period_days}μ)</div></div>
    <div class="grid">
      <div class="stat"><div class="v">${s.recovered_count||0}</div><div class="l">ραντεβού επανακτήθηκαν ✅</div></div>
      <div class="stat"><div class="v">${s.ai_bookings}</div><div class="l">ραντεβού έκλεισε το AI (30μ)</div></div>
      <div class="stat"><div class="v">${s.noshow_recovery_sent}</div><div class="l">no-shows κυνηγήθηκαν</div></div>
      <div class="stat"><div class="v">€${Math.round(ts.cumulative_revenue)}</div><div class="l">συνολική αξία ραντεβού (all-time)</div></div>
    </div>
    <div class="card"><h3>Εκτιμώμενα έσοδα ανά μήνα</h3><canvas id="rev"></canvas></div>
    <div class="card"><h3>Ραντεβού vs No-shows</h3><canvas id="bk"></canvas></div>`;
  const gridC={color:'#eef2f7'}, tickC={color:'#94a3b8',font:{size:11}};
  new Chart(document.getElementById('rev'),{type:'bar',
    data:{labels:months,datasets:[{label:'€',data:ts.months.map(m=>m.revenue),backgroundColor:'#059669',borderRadius:6}]},
    options:{plugins:{legend:{display:false}},scales:{y:{grid:gridC,ticks:tickC},x:{grid:{display:false},ticks:tickC}}}});
  new Chart(document.getElementById('bk'),{type:'line',
    data:{labels:months,datasets:[
      {label:'Ραντεβού',data:ts.months.map(m=>m.bookings),borderColor:'#2563eb',tension:.3},
      {label:'No-shows',data:ts.months.map(m=>m.no_shows),borderColor:'#dc2626',tension:.3}]},
    options:{plugins:{legend:{labels:{color:'#475569',font:{size:11}}}},scales:{y:{grid:gridC,ticks:tickC},x:{grid:{display:false},ticks:tickC}}}});
}).catch(()=>{document.getElementById('content').innerHTML='<div class="err">Σφάλμα φόρτωσης</div>';});
</script></body></html>"""


@bp.get("/<slug>/dashboard")
def customer_dashboard(slug):
    """Live customer-facing €-dashboard — ο γιατρός βλέπει 24/7 πόσα € του γύρισε το AI.
    Το #1 retention όπλο + demo που πουλάει. HMAC-gated ανά slug (όχι admin token)."""
    business = db.get_business(slug)
    if not business:
        abort(404)
    if not _check_dash_token(slug):
        return "Μη εξουσιοδοτημένη πρόσβαση", 403
    try:
        from routes.billing_routes import subscription_blocked, paywall_response
        if subscription_blocked(slug):
            return paywall_response()
    except Exception:
        pass
    html = (_DASHBOARD_HTML
            .replace("__NAME__", (business.get("name") or "").replace("<", "").replace(">", ""))
            .replace("__SLUG__", slug)
            .replace("__TOKEN__", dash_token(slug)))
    return html


def register(app):
    app.register_blueprint(bp)
