"""
agent_routes.py — Agentic Employee panel (propose→approve action queue)

Ο «ψηφιακός υπάλληλος»: σκανάρει τα δεδομένα του ιατρείου (agentic_employee.py)
και δείχνει στον γιατρό μια ΟΥΡΑ ΕΝΕΡΓΕΙΩΝ. Ο γιατρός πατάει «Έγκριση» σε καθεμία.

ΝΟΜΙΚΟ (Ν.3471/2006): ο υπάλληλος ΔΕΝ στέλνει μηνύματα μόνος του. Δρα ΜΟΝΟ
μετά από ρητή έγκριση ανθρώπου. Σε v1 το «Έγκριση» ΚΑΤΑΓΡΑΦΕΙ την ενέργεια·
η πραγματική αποστολή (SMS/Viber) ενεργοποιείται ΞΕΧΩΡΙΣΤΑ όταν υπάρχει
SMS provider + συναίνεση ασθενούς (δεν auto-blast-άρει σε v1).

Wiring (μετά από review): στο app.py βάλε
    from routes.agent_routes import register as _register_agent
    _register_agent(app)

Routes (token-gated, ίδιο token με back-office):
  GET  /api/<slug>/agent/queue    → JSON ουρά ενεργειών
  GET  /<slug>/agent              → HTML panel (approve UI)
  POST /api/<slug>/agent/approve  → καταγράφει έγκριση (v1: log, χωρίς auto-send)
"""
import json
from flask import Blueprint, request, jsonify, render_template_string

try:
    from agentic_employee import build_action_queue
except Exception:
    def build_action_queue(slug):
        return {"slug": slug, "briefing": {"headline": "agentic module not loaded", "total_actions": 0,
                "by_type": {}, "eur_at_stake": 0}, "actions": []}

bp = Blueprint("agent", __name__)


def _gate(slug):
    """Owner cookie ή dashboard HMAC token — ίδιο με το back-office."""
    try:
        from routes.back_office_routes import _check_token
        return _check_token(slug)
    except Exception:
        # fail-closed αν δεν φορτώνει το gate
        return False


@bp.get("/api/<slug>/agent/queue")
def agent_queue(slug):
    if not _gate(slug):
        return jsonify({"error": "unauthorized"}), 403
    return jsonify(build_action_queue(slug))


@bp.post("/api/<slug>/agent/approve")
def agent_approve(slug):
    """Ο γιατρός ενέκρινε — η έγκρισή του ΕΙΝΑΙ η νόμιμη εξουσιοδότηση (Ν.3471/2006) να
    σταλεί το μήνυμα. Εκτελεί μέσω back_office_assistant.execute_action (ίδιο write-path
    με τον AI Γραμματέα) — email αν υπάρχει στον φάκελο, αλλιώς SMS αν είναι configured.
    Idempotent: διπλό κλικ στο ίδιο action δεν ξαναστέλνει (request_id σταθερό ανά action)."""
    if not _gate(slug):
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True, force=True) or {}
    action_type = data.get("type", "")
    name = data.get("name", "")
    phone = (data.get("phone") or "").strip()
    message = (data.get("proposed_message") or "").strip()
    detail = data.get("detail", "")
    if not phone or not message:
        return jsonify({"ok": False, "error": "Λείπει τηλέφωνο ή προτεινόμενο μήνυμα"}), 400

    from back_office_assistant import execute_action
    rid = f"agent-approve:{slug}:{action_type}:{phone}:{detail}"
    result = execute_action(slug, "send_message", {"phone": phone, "text": message}, request_id=rid)
    print(f"[AGENT APPROVE] slug={slug} type={action_type} name={name} phone={phone[:6]}*** "
          f"→ ok={result.get('ok')} {result.get('msg') or result.get('error') or ''}")
    return jsonify({
        "ok": bool(result.get("ok")),
        "status": "sent" if result.get("ok") else "failed",
        "note": result.get("msg") or result.get("error") or "",
    })


@bp.get("/<slug>/agent")
def agent_panel(slug):
    if not _gate(slug):
        return "Unauthorized", 403
    q = build_action_queue(slug)
    return render_template_string(_PANEL, slug=slug, q=q, q_json=json.dumps(q, ensure_ascii=False))


@bp.get("/api/admin/bridge/demo")
def bridge_demo():
    """DEMO: τρέχει έναν πλήρη κύκλο ασθενή μέσα από τους 5 υπαλλήλους (booking→υποδοχή→κλινικός→
    λογιστήριο→αναχώρηση) σε ΚΑΘΑΡΟ demo ιατρείο (reset πρώτα — ΔΕΝ αγγίζει πραγματικά δεδομένα
    πελατών). Επιστρέφει το ίχνος επικοινωνίας (ποιος υπάλληλος, τι, με ποια σειρά)."""
    from admin_auth import admin_ok, token_from_request
    if not admin_ok(token_from_request()):
        return jsonify({"error": "unauthorized"}), 401
    import provisioning, database as db
    from clinic_bridge import ClinicBridge
    from datetime import datetime, timedelta
    slug = "bridge-demo"
    provisioning.provision("Επίδειξη Γέφυρας", "physio", owner_name="Demo",
                           email="demo@ran-tevu.gr", demo=False, slug=slug)
    provisioning.reset_business_data(slug)
    db.log_consent(consent_type="reminders", slug=slug, identifier="6970000000", accepted=True)
    tom = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    br = ClinicBridge(slug)
    br.request_booking("Δοκιμαστικός Ασθενής", "6970000000", "10:00", tom, value=40.0)
    booked = next((e for e in br.journey() if e["type"] == "appointment.booked"), None)
    if booked:
        bid = booked["payload"]["booking_id"]
        br.arrive(bid, "Δοκιμαστικός Ασθενής", "6970000000")
        br.complete_visit(bid, "Δοκιμαστικός Ασθενής", "6970000000",
                          notes="Οσφυαλγία 5 ημερών. Πρόταση: φυσικοθεραπεία.", value=40.0)
    labels = {"booking": "Ρεσεψιόν", "reception": "Υποδοχή", "clinical": "Κλινικός",
              "billing": "Λογιστήριο", "checkout": "Αναχώρηση", "external": "Ασθενής"}
    steps = [{"seq": e["seq"], "event": e["type"], "agent": labels.get(e["source"], e["source"])}
             for e in br.journey()]
    with db._conn() as c:
        n_inv = c.execute("SELECT COUNT(*) FROM bo_invoices WHERE slug=?", (slug,)).fetchone()[0]
    return jsonify({"steps": steps, "invoices": n_inv,
                    "appointments": len(db.get_all_bookings(slug))})


def register(app):
    app.register_blueprint(bp)
    print("[AGENT] agentic panel loaded — /<slug>/agent")


_PANEL = r"""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="robots" content="noindex,nofollow"/>
<title>Ψηφιακός Υπάλληλος — {{ q.briefing.business if q.briefing.business else slug }}</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f3f6f6;color:#1f2937;padding:24px 16px}
  .wrap{max-width:760px;margin:0 auto}
  .hero{background:linear-gradient(135deg,#0F7B8A,#13a4b8);color:#fff;border-radius:18px;padding:24px 26px;margin-bottom:18px}
  .hero .k{font-size:12px;text-transform:uppercase;letter-spacing:1px;opacity:.85}
  .hero h1{font-size:22px;margin:6px 0 4px}
  .hero .eur{font-size:30px;font-weight:800;margin-top:8px}
  .stat{display:inline-block;background:rgba(255,255,255,.15);border-radius:20px;padding:3px 12px;font-size:13px;margin:8px 6px 0 0}
  .card{background:#fff;border-radius:14px;padding:16px 18px;margin-bottom:10px;box-shadow:0 4px 18px rgba(0,0,0,.05);
        display:flex;align-items:center;gap:14px}
  .tag{font-size:11px;font-weight:800;padding:3px 9px;border-radius:20px;white-space:nowrap}
  .recall{background:#e6f4f1;color:#0F7B8A}.reactivate{background:#fef3e2;color:#b45309}.noshow{background:#fee2e2;color:#b91c1c}
  .who{font-weight:700}.det{font-size:13px;color:#6b7280}
  .grow{flex:1;min-width:0}
  .eur2{font-weight:800;color:#0F7B8A;white-space:nowrap}
  .btn{background:#0F7B8A;color:#fff;border:0;border-radius:10px;padding:9px 16px;font-weight:700;cursor:pointer;white-space:nowrap}
  .btn:disabled{opacity:.5;cursor:default}
  .empty{text-align:center;color:#6b7280;padding:40px}
  .legal{font-size:12px;color:#9ca3af;margin-top:16px;text-align:center}
</style></head><body><div class="wrap">
  <div class="hero">
    <div class="k">Ψηφιακός Υπάλληλος · {{ q.briefing.business if q.briefing.business else slug }}</div>
    <h1>{{ q.briefing.headline }}</h1>
    {% if q.briefing.eur_at_stake %}<div class="eur">έως €{{ q.briefing.eur_at_stake }}</div>{% endif %}
    {% for k,v in q.briefing.by_type.items() %}<span class="stat">{{ v }} {{ k }}</span>{% endfor %}
  </div>

  {% if q.actions %}
    {% for a in q.actions %}
    <div class="card" id="card-{{ loop.index }}">
      <span class="tag {{ a.type }}">{{ a.type }}</span>
      <div class="grow">
        <div class="who">{{ a.name }}</div>
        <div class="det">{{ a.detail }} · {{ a.phone }}</div>
      </div>
      <span class="eur2">€{{ a.est_eur }}</span>
      <button class="btn" onclick='approve(this,{{ a|tojson }})'>Έγκριση →</button>
    </div>
    {% endfor %}
  {% else %}
    <div class="empty">✅ Καμία εκκρεμότητα — όλα ήρεμα σήμερα.</div>
  {% endif %}

  <div class="legal">Ο υπάλληλος προτείνει· εσύ εγκρίνεις. Καμία αποστολή χωρίς την έγκρισή σου (Ν.3471).</div>
</div>
<script>
async function approve(btn, action){
  btn.disabled=true; btn.textContent='Εγκρίθηκε ✓';
  try{
    const slug={{ slug|tojson }};
    await fetch('/api/'+slug+'/agent/approve'+location.search,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(action)});
  }catch(e){ btn.textContent='Σφάλμα'; btn.disabled=false; }
}
</script></body></html>"""
