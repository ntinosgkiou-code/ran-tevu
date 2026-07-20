"""
routes/growth_routes.py — `/api/admin/growth/*` + `/admin/growth` dashboard.

Mounted στο app μέσω register(app). Όλα τα API endpoints είναι behind
require_master (Bearer token από ADMIN_TOKEN).

Endpoints:
  GET  /admin/growth                    → HTML dashboard
  GET  /api/admin/growth/posts          → list (filter by status)
  POST /api/admin/growth/posts/<id>/approve
  POST /api/admin/growth/posts/<id>/reject
  POST /api/admin/growth/posts/<id>/edit  (body, hashtags)
  GET  /api/admin/growth/stats          → funnel + audience stats
  POST /admin/growth/quick-action       → 1-click links από email

Content generation/publishing (outreach.*, video.*) αφαιρέθηκαν 7/7/2026 — τα packages
που τα υλοποιούσαν δεν υπήρχαν ποτέ στο repo (πάντα 404/500). Τα read-only routes πάνω
(list/approve/reject/edit/stats) + το SEO blog (seo.blog_generator, ξεχωριστό, δουλεύει)
παραμένουν.
"""

from __future__ import annotations

import os
import functools
from pathlib import Path

from flask import Blueprint, request, jsonify, redirect, send_from_directory

import database as db


# ════════════════════════════════════════════════════════════════════
#  Auth
# ════════════════════════════════════════════════════════════════════

def require_master(f):
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        token = os.environ.get("ADMIN_TOKEN", "")
        if token:
            auth = request.headers.get("Authorization", "")
            # Allow query param ?token= for email 1-click links
            if auth != f"Bearer {token}":
                qtoken = request.args.get("token", "")
                if qtoken != token:
                    return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapped


bp = Blueprint("growth", __name__)


# ════════════════════════════════════════════════════════════════════
#  Dashboard HTML
# ════════════════════════════════════════════════════════════════════

@bp.get("/admin/growth")
def admin_growth_html():
    """Serves the admin dashboard HTML."""
    public_dir = Path(__file__).parent.parent / "public"
    return send_from_directory(str(public_dir), "admin_growth.html")


@bp.get("/admin/growth/quick-action")
def quick_action_redirect():
    """1-click links από approval email — redirects σε dashboard με action."""
    action = request.args.get("action", "")
    pid = request.args.get("id", "")
    return redirect(f"/admin/growth?action={action}&id={pid}")


# ════════════════════════════════════════════════════════════════════
#  Posts endpoints
# ════════════════════════════════════════════════════════════════════

@bp.get("/api/admin/growth/posts")
@require_master
def list_posts():
    status = request.args.get("status", "pending")
    platform = request.args.get("platform")
    limit = int(request.args.get("limit", "50"))

    if status == "pending":
        posts = db.list_pending_posts(platform=platform, limit=limit)
    elif status == "approved":
        posts = db.list_approved_posts(limit=limit)
    elif status == "published":
        posts = db.list_recent_posts(limit=limit)
    else:
        # raw query
        with db._conn() as conn:
            q = "SELECT * FROM content_posts WHERE status=? ORDER BY generated_at DESC LIMIT ?"
            params = [status, limit]
            if platform:
                q = "SELECT * FROM content_posts WHERE status=? AND platform=? ORDER BY generated_at DESC LIMIT ?"
                params = [status, platform, limit]
            rows = conn.execute(q, params).fetchall()
            posts = [dict(r) for r in rows]

    return jsonify({"posts": posts, "count": len(posts)})


@bp.get("/api/admin/growth/posts/<int:pid>")
@require_master
def get_post(pid):
    p = db.get_post(pid)
    if not p:
        return jsonify({"error": "not_found"}), 404
    return jsonify(p)


@bp.post("/api/admin/growth/posts/<int:pid>/approve")
@require_master
def approve_post(pid):
    p = db.get_post(pid)
    if not p:
        return jsonify({"error": "not_found"}), 404
    db.update_post_status(pid, "approved", approved_by="admin")
    return jsonify({"ok": True, "id": pid, "status": "approved"})


@bp.post("/api/admin/growth/posts/<int:pid>/reject")
@require_master
def reject_post(pid):
    db.update_post_status(pid, "rejected")
    return jsonify({"ok": True, "id": pid, "status": "rejected"})


@bp.post("/api/admin/growth/posts/<int:pid>/edit")
@require_master
def edit_post(pid):
    data = request.get_json(silent=True) or {}
    body = data.get("body", "").strip()
    hashtags = data.get("hashtags", "").strip()
    if not body:
        return jsonify({"error": "empty_body"}), 400
    db.update_post_status(pid, "pending", body=body, hashtags=hashtags)
    return jsonify({"ok": True, "id": pid})


@bp.post("/api/admin/growth/blog/generate")
@require_master
def trigger_blog_generation():
    """Manually generate 1 SEO blog post."""
    from seo.blog_generator import generate_weekly_post
    result = generate_weekly_post()
    return jsonify(result)


@bp.get("/api/admin/growth/blog/list")
@require_master
def list_blog_posts():
    """List all published blog articles."""
    from seo.blog_generator import list_posted
    return jsonify({"posts": list_posted()})


@bp.get("/blog")
def blog_index():
    """Public blog index — auto-generated από directory listing."""
    from seo.blog_generator import list_posted
    posts = list_posted()
    domain = os.environ.get("RAN_TEVU_DOMAIN", "https://www.ran-tevu.gr").rstrip("/")
    items_html = ""
    for p in posts[:50]:
        items_html += (
            f'<li style="margin-bottom:16px;padding:16px;background:#faf8ff;border-radius:8px;">'
            f'<a href="/blog/{p["slug"]}" style="color:#4a148c;font-weight:600;font-size:18px;text-decoration:none;">{p["slug"].replace("-", " ").title()}</a>'
            f'<div style="color:#888;font-size:12px;margin-top:4px;">{p["modified"][:10]}</div>'
            f'</li>'
        )
    if not items_html:
        items_html = '<li style="color:#888;text-align:center;padding:40px;">Σύντομα — πρώτο άρθρο τη Δευτέρα.</li>'
    return f"""<!DOCTYPE html>
<html lang="el">
<head>
<meta charset="UTF-8">
<title>Ran-Tevu Blog · AI για Ελληνικές Επιχειρήσεις</title>
<style>
  body {{ font-family: system-ui; max-width: 720px; margin: 0 auto; padding: 24px; background: #fff; color: #1a1a2e; }}
  header {{ background: linear-gradient(135deg,#1a1a2e,#4a148c); color: white; padding: 40px 24px; border-radius: 12px; margin-bottom: 32px; }}
  header h1 {{ font-size: 32px; }}
  header p {{ opacity: 0.85; margin-top: 8px; }}
  ul {{ list-style: none; padding: 0; }}
  footer {{ margin-top: 48px; padding-top: 24px; border-top: 1px solid #eee; text-align: center; font-size: 13px; color: #888; }}
  footer a {{ color: #4a148c; }}
</style>
</head>
<body>
<header>
  <h1>📚 Ran-Tevu Blog</h1>
  <p>SEO articles για Greek SMBs — AI, automation, growth</p>
</header>
<ul>{items_html}</ul>
<section style="margin:40px 0 8px;padding:28px 24px;border:1px solid #e7e5e4;border-radius:16px;background:#fafaf9;text-align:center">
  <h3 style="font-size:20px;margin:0 0 6px;color:#1c1917">Θες τέτοια κάθε Τρίτη στο inbox σου;</h3>
  <p style="font-size:14.5px;color:#57534e;margin:0 0 16px">Το δωρεάν newsletter για AI στην ιατρική — 4 λεπτά, χωρίς hype, στα ελληνικά.</p>
  <form onsubmit="var e=this.email.value,m=this.querySelector('.nlm');m.textContent='...';fetch('/api/newsletter/subscribe',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:e}})}}).then(r=>r.json()).then(d=>{{m.textContent=d.ok?'Έγινε! Τα λέμε την Τρίτη.':(d.msg||'Δοκίμασε ξανά.');if(d.ok)this.reset();}});return false;" style="display:flex;gap:8px;flex-wrap:wrap;justify-content:center">
    <input name="email" type="email" required placeholder="Το email σου" style="flex:1;min-width:200px;max-width:300px;padding:11px 13px;border:1px solid #d6d3d1;border-radius:9px;font-size:14.5px">
    <button type="submit" style="padding:11px 20px;background:#1c1917;color:#fff;border:none;border-radius:9px;font-weight:600;font-size:14px;cursor:pointer">Εγγραφή</button>
  </form>
  <p class="nlm" style="font-size:12.5px;color:#0d9488;margin:10px 0 0;min-height:16px"></p>
</section>
<footer>
  <p><a href="/">← Ran-Tevu</a> · <a href="/services">Services</a> · <a href="/store">Store</a></p>
</footer>
</body>
</html>"""


@bp.get("/blog/<slug>")
def blog_post(slug):
    """Serve individual blog post."""
    from pathlib import Path
    from flask import send_file
    safe = "".join(c for c in slug if c.isalnum() or c in "-_")
    path = Path(__file__).parent.parent / "public" / "blog" / f"{safe}.html"
    if not path.exists():
        return jsonify({"error": "post_not_found"}), 404
    return send_file(str(path))


# ════════════════════════════════════════════════════════════════════
#  Stats
# ════════════════════════════════════════════════════════════════════

@bp.get("/api/admin/growth/stats")
@require_master
def stats():
    days = int(request.args.get("days", "30"))
    with db._conn() as conn:
        # Posts breakdown
        rows = conn.execute("""
            SELECT status, platform, language, COUNT(*) as cnt
            FROM content_posts
            WHERE generated_at >= datetime('now', '-30 days')
            GROUP BY status, platform, language
        """).fetchall()
        posts_breakdown = [dict(r) for r in rows]

        # Funnel
        funnel = db.funnel_stats(days=days)

        # Audience
        growth = db.get_audience_growth(days=days)

        # Recent published
        recent = conn.execute("""
            SELECT id, platform, language, body, published_at, impressions, clicks, conversions
            FROM content_posts
            WHERE status='published'
            ORDER BY published_at DESC LIMIT 10
        """).fetchall()
        recent_published = [dict(r) for r in recent]

    return jsonify({
        "posts_breakdown": posts_breakdown,
        "funnel": funnel,
        "audience_growth": growth,
        "recent_published": recent_published,
    })


# ════════════════════════════════════════════════════════════════════
#  Funnel tracking (called from public-facing pages via UTM)
# ════════════════════════════════════════════════════════════════════

@bp.post("/api/growth/track")
def track_event():
    """Public endpoint — tracks funnel events. No auth required.
    Body: {event_type, source, campaign?, lead_email?, metadata?}"""
    data = request.get_json(silent=True) or {}
    if not data.get("event_type") or not data.get("source"):
        return jsonify({"error": "missing_fields"}), 400
    db.log_funnel_event(
        event_type=data["event_type"],
        source=data["source"],
        campaign=data.get("campaign", ""),
        lead_email=data.get("lead_email", ""),
        lead_country=data.get("lead_country", ""),
        metadata=data.get("metadata", {}),
    )
    return jsonify({"ok": True})


def register(app):
    """Mount the Growth blueprint on the given Flask app."""
    app.register_blueprint(bp)
