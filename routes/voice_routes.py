"""
Flask blueprint για το Voice Agent system.

Endpoints:
- POST /api/voice/webhook/<slug>           — Vapi callbacks (function calls, end-of-call)
- POST /api/admin/voice/provision/<slug>   — admin: create/update voice agent
- DELETE /api/admin/voice/provision/<slug> — admin: deprovision
- GET  /api/admin/voice/healthcheck        — check all 3 APIs
- GET  /api/admin/voice/<slug>/calls       — list calls + transcripts
- GET  /api/admin/voice/<slug>/cost        — monthly cost summary
- POST /api/admin/voice/test-call/<slug>   — trigger outbound test call
- GET  /admin/<slug>/voice                 — admin UI for voice config
"""
from flask import Blueprint, request, jsonify, render_template_string, abort
from functools import wraps
import hmac
import os
import database as db
from voice import (
    vapi_client, twilio_client, provisioner, webhook_handler,
)
from voice import elevenlabs_client, cost_tracker
from voice.assistant_builder import build_assistant_config
from admin_auth import require_master

bp = Blueprint("voice", __name__)


def require_webhook_secret(fn):
    """Soft-validate Vapi server URL secret. Vapi sends it as header."""
    @wraps(fn)
    def wrap(*args, **kwargs):
        is_prod = bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_ENVIRONMENT"))
        # No hardcoded secret in source (it would be public in git). Production MUST set
        # VAPI_WEBHOOK_SECRET; local dev falls back to a dev-only value.
        expected = os.environ.get("VAPI_WEBHOOK_SECRET", "").strip()
        if not expected:
            if is_prod:
                # FAIL-CLOSED: an unprotected webhook lets anyone who knows a slug POST forged
                # transcripts/bookings. Refuse rather than accept unauthenticated in production.
                print("[voice-webhook] BLOCKED: VAPI_WEBHOOK_SECRET not set in production — set it in Railway + Vapi.")
                return jsonify({"error": "webhook not configured"}), 503
            expected = "ran-tevu-dev-local"  # local dev only
        sent = (
            request.headers.get("X-Vapi-Secret")
            or request.headers.get("x-vapi-server-url-secret")
            or request.args.get("secret")
            or ""
        ).strip()
        # constant-time comparison (avoid timing side-channel on the shared secret)
        if not hmac.compare_digest(sent, expected):
            strict = os.environ.get("VAPI_STRICT_WEBHOOK", "1" if is_prod else "0") == "1"
            if strict:
                print(f"[voice-webhook] BLOCKED: secret mismatch (got '{sent[:10]}')")
                return jsonify({"error": "invalid webhook secret"}), 403
            print(f"[voice-webhook] WARNING (dev): secret mismatch (got '{sent[:10]}')")
        return fn(*args, **kwargs)
    return wrap


# ── Vapi webhook (PUBLIC — secret-protected) ──────────────────────────

@bp.post("/api/voice/webhook/<slug>")
@require_webhook_secret
def vapi_webhook(slug):
    payload = request.get_json(silent=True) or {}
    try:
        response = webhook_handler.handle_vapi_webhook(slug, payload)
        return jsonify(response)
    except Exception as e:
        print(f"[voice-webhook] FATAL: {e}")
        # Always return 200 to Vapi — failures shouldn't break the call
        return jsonify({"result": "Συγγνώμη, είχα τεχνικό πρόβλημα."})


# ── Admin: provisioning ──────────────────────────────────────────────

@bp.post("/api/admin/voice/provision/<slug>")
@require_master
def provision(slug):
    """Body (all optional):
    {
      "voice_key": "madlen|stefanos|athina|dimitris",
      "assistant_name": "Madlen",
      "twilio_phone": "+30...",
      "purchase_new_number": false,
      "area_code": "2831",
      "dry_run": false
    }"""
    data = request.get_json(silent=True) or {}
    try:
        result = provisioner.provision_voice_agent(
            business_slug=slug,
            voice_key=data.get("voice_key"),
            assistant_name=data.get("assistant_name", "Madlen"),
            twilio_phone=data.get("twilio_phone"),
            purchase_new_number=data.get("purchase_new_number", False),
            area_code=data.get("area_code", "2831"),
            dry_run=data.get("dry_run", False),
        )
        return jsonify(result)
    except provisioner.ProvisionError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Unexpected: {e}"}), 500


@bp.delete("/api/admin/voice/provision/<slug>")
@require_master
def deprovision(slug):
    release = request.args.get("release_number", "false").lower() == "true"
    try:
        return jsonify(provisioner.deprovision_voice_agent(slug, release_twilio_number=release))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Admin: monitoring ────────────────────────────────────────────────

@bp.get("/api/admin/voice/healthcheck")
@require_master
def healthcheck():
    return jsonify({
        "vapi": vapi_client.healthcheck(),
        "twilio": twilio_client.healthcheck(),
        "elevenlabs": elevenlabs_client.healthcheck(),
        "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "webhook_url_base": os.environ.get("PUBLIC_BASE_URL", "https://www.ran-tevu.gr"),
    })


@bp.get("/api/admin/voice/<slug>/calls")
@require_master
def list_calls(slug):
    limit = int(request.args.get("limit", 50))
    with db._conn() as c:
        rows = c.execute("""
            SELECT id, vapi_call_id, from_number, started_at, duration_seconds,
                   cost_usd, summary, success_evaluation, recording_url, ended_reason
            FROM voice_calls
            WHERE business_slug=?
            ORDER BY started_at DESC LIMIT ?
        """, (slug, limit)).fetchall()
    from crypto_util import decrypt as _dec
    out = []
    for r in rows:
        d = dict(r)
        if d.get("summary"):
            d["summary"] = _dec(d["summary"])
        out.append(d)
    return jsonify(out)


@bp.get("/api/admin/voice/<slug>/call/<int:call_id>")
@require_master
def call_detail(slug, call_id):
    with db._conn() as c:
        row = c.execute(
            "SELECT * FROM voice_calls WHERE id=? AND business_slug=?",
            (call_id, slug),
        ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    from crypto_util import decrypt as _dec
    d = dict(row)
    for k in ("transcript", "summary"):
        if d.get(k):
            d[k] = _dec(d[k])
    return jsonify(d)


@bp.get("/api/admin/voice/<slug>/cost")
@require_master
def cost(slug):
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    return jsonify(cost_tracker.monthly_cost_for_business(slug, year, month))


@bp.get("/api/admin/voice/cost-summary")
@require_master
def cost_summary():
    return jsonify(cost_tracker.all_businesses_monthly_summary())


@bp.get("/api/admin/voice/<slug>/status")
@require_master
def status(slug):
    agent = provisioner.get_voice_agent(slug)
    if not agent:
        return jsonify({"provisioned": False})
    return jsonify({
        "provisioned": True,
        "agent": {k: v for k, v in agent.items() if k != "config_json"},
        "quota_check": cost_tracker.check_quota_alert(slug),
    })


@bp.post("/api/admin/voice/<slug>/preview-config")
@require_master
def preview_config(slug):
    """Returns the Vapi assistant config that WOULD be sent (dry run)."""
    data = request.get_json(silent=True) or {}
    try:
        result = provisioner.provision_voice_agent(
            slug,
            voice_key=data.get("voice_key"),
            assistant_name=data.get("assistant_name", "Madlen"),
            dry_run=True,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── Admin UI ─────────────────────────────────────────────────────────

ADMIN_UI = """<!DOCTYPE html>
<html lang="el"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice Agent — {{ slug }}</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 1100px;
         margin: 20px auto; padding: 20px; background: #0a0a0f; color: #e5e7eb; }
  h1 { color: #a78bfa; }
  h2 { color: #c4b5fd; margin-top: 30px; border-bottom: 1px solid #374151; padding-bottom: 8px; }
  .card { background: #111827; border: 1px solid #1f2937; border-radius: 12px;
          padding: 18px; margin: 14px 0; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  label { display: block; font-size: 13px; margin-top: 10px; color: #9ca3af; }
  input, select, textarea {
    width: 100%; padding: 8px 12px; background: #1f2937; border: 1px solid #374151;
    border-radius: 6px; color: white; font-size: 14px;
  }
  button { background: linear-gradient(135deg, #7c3aed, #ec4899);
           color: white; padding: 10px 22px; border: none; border-radius: 8px;
           cursor: pointer; font-weight: 600; margin-top: 12px; }
  button:hover { opacity: 0.9; }
  button.danger { background: #dc2626; }
  pre { background: #030712; padding: 12px; border-radius: 6px; overflow-x: auto;
        font-size: 12px; color: #d1d5db; }
  .status-ok { color: #10b981; }
  .status-err { color: #ef4444; }
  .call-row { display: grid; grid-template-columns: 80px 100px 80px 1fr 80px;
              gap: 10px; padding: 10px; border-bottom: 1px solid #1f2937; font-size: 13px; }
  .badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; }
  .badge.success { background: #064e3b; color: #6ee7b7; }
  .badge.fail { background: #7f1d1d; color: #fca5a5; }
</style>
</head><body>

<h1>🎙️ Voice Agent — {{ slug }}</h1>

<div class="card" id="status-card">Loading status...</div>

<h2>⚙️ Configuration</h2>
<div class="card">
  <div class="grid">
    <div>
      <label>Voice (Φωνή)</label>
      <select id="voice_key">
        <option value="madlen">Madlen — γυναικεία, ζεστή (medical/wellness)</option>
        <option value="athina">Athina — γυναικεία, νεανική (fitness/hair)</option>
        <option value="stefanos">Stefanos — αντρική, professional (restaurant/legal)</option>
        <option value="dimitris">Dimitris — αντρική, mature (dental/vet)</option>
      </select>
    </div>
    <div>
      <label>Όνομα assistant (πώς συστήνεται)</label>
      <input id="assistant_name" value="Madlen" />
    </div>
    <div>
      <label>Twilio phone (αν έχεις ήδη — αλλιώς άφησε κενό)</label>
      <input id="twilio_phone" placeholder="+30..." />
    </div>
    <div>
      <label>Αγορά νέου νούμερου;</label>
      <select id="purchase_new_number">
        <option value="false">Όχι, χρησιμοποίησε υπάρχον</option>
        <option value="true">Ναι, αγόρασε νέο (~$1/μήνα)</option>
      </select>
    </div>
  </div>
  <button onclick="previewConfig()">📋 Preview config (dry run)</button>
  <button onclick="provision()">🚀 Provision / Update</button>
  <button class="danger" onclick="deprovision()">🗑️ Deprovision</button>
  <pre id="provision_output" style="display:none"></pre>
</div>

<h2>📞 Recent Calls</h2>
<div class="card" id="calls-card">Loading...</div>

<h2>💰 Monthly Cost</h2>
<div class="card" id="cost-card">Loading...</div>

<script>
const SLUG = "{{ slug }}";
const TOKEN = new URLSearchParams(location.search).get("token") || "";

function authHeaders() {
  return { "Authorization": "Bearer " + TOKEN, "Content-Type": "application/json" };
}

async function loadStatus() {
  const r = await fetch(`/api/admin/voice/${SLUG}/status`, { headers: authHeaders() });
  const data = await r.json();
  const el = document.getElementById("status-card");
  if (!data.provisioned) {
    el.innerHTML = `<strong class="status-err">⚠ Δεν έχει provisioned voice agent.</strong>`;
    return;
  }
  const a = data.agent;
  const q = data.quota_check;
  el.innerHTML = `
    <strong class="status-ok">✅ Active</strong> ·
    📞 ${a.twilio_phone_number || "(no number)"} ·
    🎤 ${a.voice_key} (${a.assistant_name}) ·
    Vapi assistant: <code>${a.vapi_assistant_id}</code>
    <br>Quota: ${q.used_minutes} / ${q.quota_minutes} min (${q.used_pct}%) — ${q.alert_level}
  `;
}

async function previewConfig() {
  const body = JSON.stringify({
    voice_key: document.getElementById("voice_key").value,
    assistant_name: document.getElementById("assistant_name").value,
    dry_run: true,
  });
  const r = await fetch(`/api/admin/voice/${SLUG}/preview-config`, {
    method: "POST", headers: authHeaders(), body
  });
  const data = await r.json();
  const out = document.getElementById("provision_output");
  out.style.display = "block";
  out.textContent = JSON.stringify(data, null, 2);
}

async function provision() {
  if (!confirm("Provision/update voice agent;")) return;
  const body = JSON.stringify({
    voice_key: document.getElementById("voice_key").value,
    assistant_name: document.getElementById("assistant_name").value,
    twilio_phone: document.getElementById("twilio_phone").value || null,
    purchase_new_number: document.getElementById("purchase_new_number").value === "true",
  });
  const r = await fetch(`/api/admin/voice/provision/${SLUG}`, {
    method: "POST", headers: authHeaders(), body
  });
  const data = await r.json();
  const out = document.getElementById("provision_output");
  out.style.display = "block";
  out.textContent = JSON.stringify(data, null, 2);
  if (data.ok) loadStatus();
}

async function deprovision() {
  if (!confirm("ΣΙΓΟΥΡΑ διαγραφή voice agent;")) return;
  const r = await fetch(`/api/admin/voice/provision/${SLUG}`, {
    method: "DELETE", headers: authHeaders()
  });
  const data = await r.json();
  alert(JSON.stringify(data, null, 2));
  loadStatus();
}

async function loadCalls() {
  const r = await fetch(`/api/admin/voice/${SLUG}/calls?limit=50`, { headers: authHeaders() });
  const calls = await r.json();
  const el = document.getElementById("calls-card");
  if (!calls.length) { el.innerHTML = "Καμία κλήση ακόμα."; return; }
  el.innerHTML = `
    <div class="call-row"><strong>Από</strong><strong>Πότε</strong><strong>Διάρκεια</strong><strong>Σύνοψη</strong><strong>Cost</strong></div>
    ${calls.map(c => `
      <div class="call-row">
        <div>${(c.from_number || "?").slice(-7)}</div>
        <div>${(c.started_at || "").slice(0, 16)}</div>
        <div>${Math.round((c.duration_seconds || 0)/6)/10}m</div>
        <div>${c.summary || c.ended_reason || "—"}</div>
        <div>$${(c.cost_usd || 0).toFixed(3)}</div>
      </div>
    `).join("")}
  `;
}

async function loadCost() {
  const r = await fetch(`/api/admin/voice/${SLUG}/cost`, { headers: authHeaders() });
  const c = await r.json();
  document.getElementById("cost-card").innerHTML = `
    <strong>${c.year}-${String(c.month).padStart(2, "0")}</strong>:
    ${c.calls} κλήσεις, ${c.total_minutes} λεπτά, ${c.voice_bookings} bookings<br>
    Vapi reported: $${c.vapi_reported_cost_usd} ·
    Δικό μας εκτίμηση: €${c.estimated_our_cost_eur}<br>
    <em>Margin at €99/flat: €${c.margin_at_99_flat_eur}</em>
  `;
}

loadStatus();
loadCalls();
loadCost();
</script>
</body></html>"""


@bp.get("/admin/<slug>/voice")
def admin_ui(slug):
    """Admin UI για voice config. Token via ?token=ADMIN_TOKEN ή cookie."""
    business = db.get_business(slug)
    if not business:
        abort(404)
    return render_template_string(ADMIN_UI, slug=slug, business=business)


__all__ = ["bp", "register"]


# ── Self-service onboarding ─────────────────────────────────────────────

@bp.post("/api/onboarding/voice/submit")
def onboarding_submit():
    """Public endpoint — receives form submission, creates business +
    provisions voice agent (if API keys configured) + sends welcome email."""
    from voice.onboarding import onboard_new_business
    payload = request.get_json(silent=True) or {}
    # Simple rate limit: 1 onboarding per IP per minute
    # (relies on Flask-Limiter being set up; soft skip otherwise)
    try:
        result = onboard_new_business(payload, provision_voice=True)
        status = 200 if result.get("ok") else 400
        return jsonify(result), status
    except Exception as e:
        return jsonify({"ok": False, "error": f"Unexpected: {e}"}), 500


@bp.get("/onboarding/voice")
def onboarding_ui():
    """Multi-step wizard για onboarding ενός νέου voice agent."""
    from flask import send_from_directory
    public_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public")
    return send_from_directory(public_dir, "onboarding_voice.html")


# ── Public demo (in-browser voice preview) ──────────────────────────────

@bp.get("/api/voice/demo/credentials")
def demo_credentials():
    """Returns Vapi PUBLIC key + demo assistant id for browser-side widget.

    Vapi has 2 key types:
    - VAPI_API_KEY (private, server-side, used για provisioning)
    - VAPI_PUBLIC_KEY (safe for browser, lets visitor's mic talk to assistant)

    Only the public key is exposed here."""
    from voice.demo_assistant import get_demo_assistant_id
    public_key = os.environ.get("VAPI_PUBLIC_KEY", "")
    assistant_id = get_demo_assistant_id()
    if not public_key or not assistant_id:
        return jsonify({
            "available": False,
            "reason": "demo not configured — set VAPI_PUBLIC_KEY + VAPI_DEMO_ASSISTANT_ID",
        })
    return jsonify({
        "available": True,
        "vapi_public_key": public_key,
        "assistant_id": assistant_id,
        "max_duration_sec": 90,
    })


@bp.post("/api/admin/voice/demo/provision")
@require_master
def admin_provision_demo():
    """One-time setup — creates the public demo assistant in Vapi."""
    from voice.demo_assistant import provision_demo
    return jsonify(provision_demo())


def register(app):
    app.register_blueprint(bp)
