"""
Self-hosted analytics — GDPR-friendly, no cookies, no third-party.

Track events from frontend → POST /api/track → stored in analytics_events table.
Dashboard at /admin/analytics shows:
- Page views breakdown
- Conversion funnel
- Top events
- Hourly traffic chart
- Real-time activity

Privacy:
- No PII stored (no email, name, exact IP)
- Anonymous session ID generated client-side
- Country detected from Cloudflare CF-IPCountry header (no IP storage)
- Auto-delete events older than 90 days
"""
from flask import Blueprint, request, jsonify, send_from_directory
from functools import wraps
import os, json
from datetime import date, datetime, timedelta
import database as db

bp = Blueprint("analytics", __name__)

_PUBLIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public")


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


# ── Ensure schema ──────────────────────────────────────────────────────

def _ensure_schema():
    with db._conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS analytics_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT,
                event_type  TEXT NOT NULL,
                page_url    TEXT,
                referrer    TEXT,
                country     TEXT,
                ua_short    TEXT,
                props_json  TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_analytics_created ON analytics_events(created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_analytics_event ON analytics_events(event_type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_analytics_session ON analytics_events(session_id)")
        c.commit()


# Call at module import (idempotent)
try:
    _ensure_schema()
except Exception as e:
    print(f"[analytics] schema init failed: {e}")


# ── Public tracking endpoint ───────────────────────────────────────────

@bp.post("/api/track")
def track():
    """Accepts JSON: {event, props, url, referrer, sid}.
    Uses sendBeacon-friendly response (204)."""
    data = request.get_json(silent=True) or {}
    event_type = (data.get("event") or "page_view")[:60]
    props = data.get("props") or {}
    page_url = (data.get("url") or "")[:300]
    referrer = (data.get("referrer") or "")[:300]
    session_id = (data.get("sid") or "")[:64]

    # Detect country from Cloudflare header (or X-Country fallback)
    country = (
        request.headers.get("CF-IPCountry")
        or request.headers.get("X-Country")
        or ""
    )[:4]
    ua = request.headers.get("User-Agent", "")
    # Short UA — just browser type
    ua_short = "other"
    if "Chrome" in ua and "Edg" not in ua:
        ua_short = "chrome"
    elif "Safari" in ua and "Chrome" not in ua:
        ua_short = "safari"
    elif "Firefox" in ua:
        ua_short = "firefox"
    elif "Edg" in ua:
        ua_short = "edge"
    if "Mobile" in ua or "Android" in ua or "iPhone" in ua:
        ua_short += "-mobile"

    try:
        with db._conn() as c:
            c.execute("""
                INSERT INTO analytics_events
                  (session_id, event_type, page_url, referrer, country, ua_short, props_json)
                VALUES (?,?,?,?,?,?,?)
            """, (
                session_id, event_type, page_url, referrer, country, ua_short,
                json.dumps(props, ensure_ascii=False)[:2000],
            ))
            c.commit()
    except Exception as e:
        print(f"[track] insert failed: {e}")
        # Always return 204 — analytics should never block UX

    return ("", 204)


# ── Admin: dashboard data feed ─────────────────────────────────────────

@bp.get("/api/admin/analytics/summary")
@require_master
def analytics_summary():
    """Returns: visitor counts, top pages, top events, funnel, hourly chart."""
    days = int(request.args.get("days", 7))
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    today = date.today().isoformat()

    with db._conn() as c:
        # Unique visitors
        unique_visitors = c.execute("""
            SELECT COUNT(DISTINCT session_id) AS n FROM analytics_events
            WHERE created_at >= ? AND session_id IS NOT NULL AND session_id != ''
        """, (since,)).fetchone()
        unique_visitors = int(unique_visitors["n"] or 0)

        # Total page views
        page_views = c.execute("""
            SELECT COUNT(*) AS n FROM analytics_events
            WHERE created_at >= ? AND event_type = 'page_view'
        """, (since,)).fetchone()
        page_views = int(page_views["n"] or 0)

        # Today's visitors
        today_visitors = c.execute("""
            SELECT COUNT(DISTINCT session_id) AS n FROM analytics_events
            WHERE created_at >= ? AND session_id IS NOT NULL AND session_id != ''
        """, (today + " 00:00:00",)).fetchone()
        today_visitors = int(today_visitors["n"] or 0)

        # Top pages
        top_pages = [dict(r) for r in c.execute("""
            SELECT page_url, COUNT(*) AS visits,
                   COUNT(DISTINCT session_id) AS uniques
            FROM analytics_events
            WHERE created_at >= ? AND event_type = 'page_view'
            GROUP BY page_url ORDER BY visits DESC LIMIT 10
        """, (since,)).fetchall()]

        # Top events (custom)
        top_events = [dict(r) for r in c.execute("""
            SELECT event_type, COUNT(*) AS count,
                   COUNT(DISTINCT session_id) AS unique_users
            FROM analytics_events
            WHERE created_at >= ? AND event_type != 'page_view'
            GROUP BY event_type ORDER BY count DESC LIMIT 15
        """, (since,)).fetchall()]

        # Hourly chart (last 24h)
        hourly = [dict(r) for r in c.execute("""
            SELECT strftime('%Y-%m-%d %H', created_at) AS hour,
                   COUNT(DISTINCT session_id) AS visitors,
                   COUNT(*) AS events
            FROM analytics_events
            WHERE created_at >= datetime('now', '-1 day')
            GROUP BY hour ORDER BY hour
        """).fetchall()]

        # Funnel: visitor → voice demo click → onboarding start → onboarding submit
        funnel_sessions = c.execute("""
            SELECT
                COUNT(DISTINCT CASE WHEN event_type = 'page_view' AND page_url = '/' THEN session_id END) AS visited_home,
                COUNT(DISTINCT CASE WHEN event_type = 'voice_demo_click' THEN session_id END) AS clicked_demo,
                COUNT(DISTINCT CASE WHEN event_type = 'onboarding_start' THEN session_id END) AS started_onboarding,
                COUNT(DISTINCT CASE WHEN event_type = 'onboarding_submit' THEN session_id END) AS completed_onboarding,
                COUNT(DISTINCT CASE WHEN event_type = 'stripe_click' THEN session_id END) AS clicked_stripe
            FROM analytics_events
            WHERE created_at >= ?
        """, (since,)).fetchone()
        funnel = dict(funnel_sessions) if funnel_sessions else {}

        # Countries breakdown
        countries = [dict(r) for r in c.execute("""
            SELECT country, COUNT(DISTINCT session_id) AS uniques
            FROM analytics_events
            WHERE created_at >= ? AND country IS NOT NULL AND country != ''
            GROUP BY country ORDER BY uniques DESC LIMIT 10
        """, (since,)).fetchall()]

        # Devices
        devices = [dict(r) for r in c.execute("""
            SELECT ua_short, COUNT(DISTINCT session_id) AS uniques
            FROM analytics_events
            WHERE created_at >= ? AND ua_short IS NOT NULL
            GROUP BY ua_short ORDER BY uniques DESC
        """, (since,)).fetchall()]

        # Referrers
        referrers = [dict(r) for r in c.execute("""
            SELECT referrer, COUNT(DISTINCT session_id) AS uniques
            FROM analytics_events
            WHERE created_at >= ?
              AND referrer IS NOT NULL AND referrer != ''
              AND referrer NOT LIKE '%ran-tevu.gr%'
            GROUP BY referrer ORDER BY uniques DESC LIMIT 10
        """, (since,)).fetchall()]

    return jsonify({
        "period_days": days,
        "totals": {
            "unique_visitors": unique_visitors,
            "page_views": page_views,
            "today_visitors": today_visitors,
        },
        "top_pages": top_pages,
        "top_events": top_events,
        "hourly_chart": hourly,
        "funnel": funnel,
        "countries": countries,
        "devices": devices,
        "referrers": referrers,
    })


@bp.get("/api/admin/analytics/realtime")
@require_master
def analytics_realtime():
    """Last 30 events for live feed."""
    with db._conn() as c:
        rows = c.execute("""
            SELECT event_type, page_url, country, ua_short, created_at, props_json
            FROM analytics_events
            ORDER BY id DESC LIMIT 30
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.post("/api/admin/analytics/cleanup")
@require_master
def analytics_cleanup():
    """Delete events older than N days (default 90)."""
    days = int(request.args.get("days", 90))
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db._conn() as c:
        cur = c.execute("DELETE FROM analytics_events WHERE created_at < ?", (cutoff,))
        c.commit()
        return jsonify({"deleted": cur.rowcount, "older_than_days": days})


# ── Admin UI ───────────────────────────────────────────────────────────

@bp.get("/admin/analytics")
def analytics_ui():
    return send_from_directory(_PUBLIC_DIR, "admin_analytics.html")


# ── Tracker JS (served standalone for easy include) ────────────────────

TRACKER_JS = """
// Ran-Tevu Analytics Tracker (privacy-first, no cookies)
(function() {
  if (window.__rtTrackerLoaded) return;
  window.__rtTrackerLoaded = true;

  // Anonymous session ID (no cookie, localStorage)
  function getSid() {
    try {
      let s = localStorage.getItem('_rt_sid');
      if (!s) {
        s = 'rt_' + Math.random().toString(36).slice(2, 12) + Date.now().toString(36);
        localStorage.setItem('_rt_sid', s);
      }
      return s;
    } catch (e) { return 'anon_' + Date.now(); }
  }

  function send(event, props) {
    const payload = JSON.stringify({
      event: event,
      props: props || {},
      url: location.pathname + location.search,
      referrer: document.referrer || '',
      sid: getSid(),
      ts: Date.now(),
    });
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon('/api/track', new Blob([payload], { type: 'application/json' }));
      } else {
        fetch('/api/track', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: payload, keepalive: true });
      }
    } catch (e) {}
  }

  // Auto: track page view on load
  send('page_view');

  // Expose for manual tracking
  window.rtTrack = send;

  // Auto-track: clicks on [data-track] elements
  document.addEventListener('click', function(e) {
    const el = e.target.closest('[data-track]');
    if (!el) return;
    const event = el.getAttribute('data-track');
    const props = {};
    for (const attr of el.attributes) {
      if (attr.name.startsWith('data-track-')) {
        props[attr.name.substring(11)] = attr.value;
      }
    }
    if (el.tagName === 'A' && el.href) props.target_url = el.href;
    send(event, props);
  }, { capture: true });

  // Track scroll depth (25%, 50%, 75%, 100%)
  const milestones = [25, 50, 75, 100];
  const seen = new Set();
  window.addEventListener('scroll', function() {
    const scrolled = (window.scrollY + window.innerHeight) / document.body.scrollHeight * 100;
    for (const m of milestones) {
      if (!seen.has(m) && scrolled >= m) {
        seen.add(m);
        send('scroll_depth', { depth: m });
      }
    }
  }, { passive: true });

  // Track form submissions
  document.addEventListener('submit', function(e) {
    const form = e.target;
    if (form.tagName === 'FORM') {
      send('form_submit', { form_id: form.id || form.name || 'unknown' });
    }
  }, { capture: true });
})();
"""


@bp.get("/tracker.js")
def tracker_js():
    from flask import Response
    resp = Response(TRACKER_JS, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


def register(app):
    app.register_blueprint(bp)
