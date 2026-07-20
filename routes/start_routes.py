"""
start_routes.py — OWNER-ONLY self-serve provisioning + futuristic UI.

ΚΛΕΙΔΩΜΕΝΟ: το /start + το provision API απαιτούν owner login (signed cookie).
Μόνο ο ιδιοκτήτης (Ντίνος) μπορεί να στήσει/δει demos — κανείς άλλος.
Password: env OWNER_PASSWORD (fallback ADMIN_TOKEN). Cookie = HMAC-signed, httponly.
"""
import os
import time
import hmac
import hashlib
from functools import wraps
from flask import Blueprint, request, jsonify, render_template_string, redirect, make_response

import database as db
from vertical_templates import VERTICAL_TEMPLATES
import provisioning

bp = Blueprint("start", __name__)

_RATE = {}
COOKIE = "rt_owner"


# ── Owner auth (signed cookie) ───────────────────────────────────────────────
def _secret() -> bytes:
    s = os.environ.get("SECRET_KEY") or os.environ.get("ADMIN_TOKEN")
    if not s:
        # FAIL-CLOSED in prod: the dev fallback is public → forgeable owner cookies.
        if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
            raise RuntimeError("Set SECRET_KEY (or ADMIN_TOKEN) in production — refusing insecure dev secret.")
        s = "ran-tevu-dev"
    return s.encode()


def _owner_token() -> str:
    return hmac.new(_secret(), b"rt-owner-gate-v2", hashlib.sha256).hexdigest()[:32]


def _owner_password() -> str:
    return os.environ.get("OWNER_PASSWORD") or os.environ.get("ADMIN_TOKEN") or ""


def is_owner() -> bool:
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), _owner_token())


def owner_required(fn):
    @wraps(fn)
    def wrap(*a, **k):
        if not is_owner():
            if request.path.startswith("/api/"):
                return jsonify({"error": "owner login required"}), 401
            return redirect("/owner")
        return fn(*a, **k)
    return wrap


def _too_fast(ip):
    now = time.time()
    last = _RATE.get(ip, 0)
    _RATE[ip] = now
    return (now - last) < 6


# ── Owner login ──────────────────────────────────────────────────────────────
@bp.get("/owner")
def owner_login_page():
    if is_owner():
        return redirect("/start")
    return render_template_string(_LOGIN_HTML, err="")


_LOGIN_FAILS = {}   # ip -> [fail_count, locked_until_ts]


def _login_ip() -> str:
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "")).split(",")[0].strip()


@bp.post("/owner")
def owner_login_submit():
    ip = _login_ip()
    rec = _LOGIN_FAILS.get(ip, [0, 0.0])
    if rec[1] > time.time():   # locked out after repeated failures
        return render_template_string(_LOGIN_HTML,
                                      err="Πολλές αποτυχημένες προσπάθειες — δοκίμασε σε λίγο."), 429
    pw = (request.form.get("password") or "").strip()
    expected = _owner_password()
    if not expected or not hmac.compare_digest(pw, expected):
        rec[0] += 1
        if rec[0] >= 5:                       # 5 fails → lock 15 min, reset counter
            rec[1] = time.time() + 15 * 60
            rec[0] = 0
        _LOGIN_FAILS[ip] = rec
        time.sleep(1.0)  # mild brute-force slowdown
        return render_template_string(_LOGIN_HTML, err="Λάθος κωδικός."), 401
    _LOGIN_FAILS.pop(ip, None)               # success → clear
    resp = make_response(redirect("/start"))
    resp.set_cookie(COOKIE, _owner_token(), max_age=60 * 60 * 24 * 30,
                    httponly=True, samesite="Lax",
                    secure=bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN")))
    return resp


@bp.get("/owner/logout")
def owner_logout():
    resp = make_response(redirect("/owner"))
    resp.delete_cookie(COOKIE)
    return resp


# ── Provision (owner-only) ───────────────────────────────────────────────────
@bp.post("/api/start/provision")
@owner_required
def start_provision():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if _too_fast(ip):
        return jsonify({"error": "Περίμενε λίγο και ξαναπροσπάθησε."}), 429
    data = request.get_json(silent=True, force=True) or {}
    vkey = (data.get("vertical") or "physio").strip()
    if vkey not in VERTICAL_TEMPLATES:
        return jsonify({"error": "Άγνωστη ειδικότητα"}), 400
    name = (data.get("name") or "").strip()[:120] or VERTICAL_TEMPLATES[vkey]['label']
    owner = (data.get("owner_name") or "").strip()[:120]
    email = (data.get("email") or "").strip()[:200]
    try:
        # demo=False → 0 fake νούμερα (ο Ντίνος δεν θέλει demo data πουθενά)· καθαρό όνομα (όχι "try"/"Demo")
        res = provisioning.provision(name, vkey, owner_name=owner, email=email, demo=False)
        return jsonify({"ok": True, **res})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)[:300]}), 500


@bp.get("/start")
@owner_required
def start_page():
    opts = [{"key": k, "label": v["label"], "focus": v.get("sales_focus", False)}
            for k, v in VERTICAL_TEMPLATES.items()]
    return render_template_string(_HTML, options=opts)


def register(app):
    app.register_blueprint(bp)
    print("[START] owner-gated self-serve loaded — /owner /start")


# ════════════════════════════════════════════════════════════════════════════
# UI — futuristic / glassmorphism + neon
# ════════════════════════════════════════════════════════════════════════════
_FX = r"""
:root{--bg:#06070d;--panel:rgba(20,24,38,.7);--line:rgba(120,140,220,.18);--txt:#eaf0ff;--mut:#8a94b8;--c1:#6d8bff;--c2:#a855f7;--c3:#22d3ee;--ok:#34f5c5;--warn:#ffd24a;--bad:#ff6b8b}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,Segoe UI,Roboto,sans-serif;min-height:100vh;overflow-x:hidden}
body::before{content:"";position:fixed;inset:0;z-index:-2;background:
  radial-gradient(60vw 60vw at 12% -5%,rgba(109,139,255,.18),transparent 60%),
  radial-gradient(50vw 50vw at 95% 8%,rgba(168,85,247,.16),transparent 60%),
  radial-gradient(45vw 45vw at 50% 110%,rgba(34,211,238,.12),transparent 60%)}
body::after{content:"";position:fixed;inset:0;z-index:-1;opacity:.5;
  background-image:linear-gradient(rgba(120,140,220,.06) 1px,transparent 1px),linear-gradient(90deg,rgba(120,140,220,.06) 1px,transparent 1px);
  background-size:44px 44px;mask-image:radial-gradient(ellipse 80% 80% at 50% 30%,#000 40%,transparent 100%)}
.glass{background:var(--panel);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border:1px solid var(--line);border-radius:20px}
.neon{background:linear-gradient(92deg,var(--c1),var(--c2) 55%,var(--c3));-webkit-background-clip:text;background-clip:text;color:transparent}
.btn{background:linear-gradient(92deg,var(--c1),var(--c2));color:#fff;border:0;border-radius:12px;font-weight:700;cursor:pointer;transition:.2s;box-shadow:0 10px 30px -8px rgba(109,139,255,.6)}
.btn:hover{transform:translateY(-2px);box-shadow:0 18px 44px -10px rgba(168,85,247,.7)}
.btn:disabled{opacity:.6;cursor:wait;transform:none}
.spin{display:inline-block;width:15px;height:15px;border:2px solid #fff;border-top-color:transparent;border-radius:50%;animation:sp .7s linear infinite;vertical-align:-2px;margin-right:8px}
@keyframes sp{to{transform:rotate(360deg)}}
"""

_LOGIN_HTML = r"""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/><meta name="robots" content="noindex,nofollow"/>
<title>Ran-Tevu · Owner</title><style>""" + _FX + r"""
.wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.box{max-width:380px;width:100%;padding:34px}
h1{font-size:22px;margin-bottom:6px}.s{color:var(--mut);font-size:13px;margin-bottom:22px}
input{width:100%;background:rgba(8,10,18,.8);border:1px solid var(--line);color:var(--txt);padding:13px 15px;border-radius:12px;font-size:15px}
input:focus{outline:none;border-color:var(--c1)}
button{width:100%;margin-top:16px;padding:13px;font-size:15px}
.err{color:var(--bad);font-size:13px;margin-top:12px;min-height:16px}
.lock{font-size:30px;margin-bottom:14px}
</style></head><body><div class="wrap"><div class="box glass">
<div class="lock">🔐</div>
<h1 class="neon">Ran-Tevu Control</h1>
<p class="s">Ιδιωτικό περιβάλλον. Μόνο εξουσιοδοτημένη πρόσβαση.</p>
<form method="post" action="/owner">
  <input type="password" name="password" placeholder="Κωδικός πρόσβασης" autofocus autocomplete="current-password"/>
  <button class="btn" type="submit">Είσοδος →</button>
  <div class="err">{{ err }}</div>
</form></div></div></body></html>"""

_HTML = r"""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/><meta name="robots" content="noindex,nofollow"/>
<title>Ran-Tevu · Στήσε σύστημα</title><style>""" + _FX + r"""
.top{display:flex;align-items:center;gap:12px;padding:18px 26px;border-bottom:1px solid var(--line)}
.top .dot{width:9px;height:9px;border-radius:50%;background:var(--ok);box-shadow:0 0 10px var(--ok)}
.top b{font-size:15px}.top .lo{margin-left:auto;color:var(--mut);font-size:12px;text-decoration:none}
.wrap{max-width:640px;margin:0 auto;padding:40px 22px}
.pill{display:inline-block;font-size:12px;color:var(--c3);border:1px solid var(--line);padding:4px 12px;border-radius:30px;margin-bottom:16px}
h1{font-size:30px;line-height:1.15;margin-bottom:10px}
.sub{color:var(--mut);line-height:1.65;margin-bottom:26px}
.card{padding:24px}
label{display:block;font-size:12px;color:var(--mut);margin:16px 0 7px;text-transform:uppercase;letter-spacing:.5px}
select,input{width:100%;background:rgba(8,10,18,.8);border:1px solid var(--line);color:var(--txt);padding:13px 15px;border-radius:12px;font-size:15px}
select:focus,input:focus{outline:none;border-color:var(--c1)}
button{width:100%;margin-top:24px;padding:15px;font-size:16px}
.err{color:var(--bad);margin-top:14px;font-size:14px;min-height:18px}
.result{display:none;margin-top:22px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:18px}
.kpi{padding:14px;text-align:center}.kpi .n{font-size:22px;font-weight:800}.kpi .l{color:var(--mut);font-size:11px;margin-top:3px}
.lnk{display:flex;align-items:center;gap:14px;padding:16px;margin-bottom:11px;text-decoration:none;color:var(--txt);transition:.2s}
.lnk:hover{border-color:var(--c1);transform:translateX(4px)}
.lnk .ic{font-size:22px}.lnk .t{font-weight:700}.lnk .d{color:var(--mut);font-size:13px}
.lnk .go{margin-left:auto;color:var(--c1)}
.feat{font-size:11px;color:var(--ok);border:1px solid rgba(52,245,197,.3);padding:1px 7px;border-radius:20px;margin-left:6px}
</style></head><body>
<div class="top"><span class="dot"></span><b class="neon">Ran-Tevu Control</b>
  <span style="color:var(--mut);font-size:12px">Provisioning Engine</span>
  <a class="lo" href="/owner/logout">Έξοδος</a></div>
<div class="wrap">
  <span class="pill">⚡ Instant provisioning · κρυπτογραφημένο · ιδιωτικό</span>
  <h1>Στήσε ένα <span class="neon">ζωντανό ιατρείο</span><br/>σε δευτερόλεπτα</h1>
  <p class="sub">Διάλεξε ειδικότητα — το σύστημα δημιουργεί αυτόματα πλήρη, λειτουργική εγκατάσταση: AI τηλεφωνική γραμματέα, πίνακα εσόδων, και ολόκληρο back-office (φάκελοι ασθενών, τιμολόγηση/myDATA, αποθήκη, agents) με δείγμα δεδομένων.</p>
  <div class="card glass" id="form">
    <label>Ειδικότητα</label>
    <select id="vertical">{% for o in options %}<option value="{{o.key}}">{{o.label}}{% if o.focus %} ⭐{% endif %}</option>{% endfor %}</select>
    <label>Όνομα ιατρείου (προαιρετικό)</label>
    <input id="name" placeholder="π.χ. Ιατρείο Παπαδόπουλου"/>
    <button class="btn" id="go" onclick="provision()">⚙️ Δημιούργησε το σύστημα</button>
    <div class="err" id="err"></div>
  </div>
  <div class="result" id="result">
    <div class="kpis" id="kpis"></div>
    <a class="lnk glass" id="lchat" target="_blank"><span class="ic">📞</span><div><div class="t">AI Γραμματέας (Front-desk)</div><div class="d">Δες πώς κλείνει ραντεβού — γράψε του</div></div><span class="go">→</span></a>
    <a class="lnk glass" id="ldash" target="_blank"><span class="ic">📊</span><div><div class="t">Πίνακας Εσόδων</div><div class="d">Πόσα λεφτά γύρισε πίσω το σύστημα</div></div><span class="go">→</span></a>
    <a class="lnk glass" id="lbo" target="_blank"><span class="ic">🧠</span><div><div class="t">Back-Office</div><div class="d">Φάκελοι, τιμολόγια/myDATA, αποθήκη, agents</div></div><span class="go">→</span></a>
    <button class="btn" onclick="location.reload()">+ Στήσε άλλο</button>
  </div>
</div>
<script>
async function provision(){
  const btn=document.getElementById('go'),err=document.getElementById('err');err.textContent='';
  btn.disabled=true;btn.innerHTML='<span class="spin"></span>Στήνω το σύστημα...';
  try{
    const r=await fetch('/api/start/provision',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({vertical:document.getElementById('vertical').value,name:document.getElementById('name').value})});
    const d=await r.json();
    if(!r.ok||!d.ok)throw new Error(d.error||'Σφάλμα');
    document.getElementById('form').style.display='none';
    const s=d.summary;
    document.getElementById('kpis').innerHTML=
      `<div class="kpi glass"><div class="n neon">${s.patients}</div><div class="l">Φάκελοι</div></div>
       <div class="kpi glass"><div class="n neon">${s.invoices}</div><div class="l">Τιμολόγια</div></div>
       <div class="kpi glass"><div class="n neon">${s.open_tasks}</div><div class="l">Εργασίες</div></div>
       <div class="kpi glass"><div class="n neon">${s.graph_links}</div><div class="l">Συνδέσεις</div></div>`;
    document.getElementById('lchat').href=d.links.chat;
    document.getElementById('ldash').href=d.links.dashboard;
    document.getElementById('lbo').href=d.links.back_office;
    document.getElementById('result').style.display='block';
  }catch(e){err.textContent='⚠️ '+e.message;btn.disabled=false;btn.innerHTML='⚙️ Δημιούργησε το σύστημα';}
}
</script></body></html>"""
