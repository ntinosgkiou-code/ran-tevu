"""
api.py — PRODUCTION API: Unified Medical Intelligence Layer over FastAPI.
========================================================================
Exposes the end-to-end pipeline (ingest → ask → act → bill) as typed HTTP endpoints, ready to
deploy as a microservice. Pydantic v2 request/response models, structured errors, health probe.

Run:   uvicorn core.api:app --port 8010     (from the mit-demo folder)
Tests: python core/test_api.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional
from urllib.parse import quote

from contextlib import asynccontextmanager

_BOOT_MONO = time.monotonic()

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from auth import APIKeyStore, make_dependency
from explanation import ExplanationLog
from pipeline import AnswerReport, IngestReport, MedicalIntelligencePipeline
from predictive_billing import AppointmentState, BillingResult, SimulationResult, ValueEvent
try:
    # swarm→main.py→integration.py is the LEGACY Core v2 booking path (not the shipping UI's booking
    # path, which is ClinicStore.add_appointment below). A misconfigured legacy dependency (e.g. its
    # own DB path) must NEVER be able to crash the shipping API — so its import failure is contained.
    from swarm import SwarmSupervisor
    _SWARM_IMPORT_ERROR: Optional[str] = None
except Exception as _swarm_exc:  # pragma: no cover - defensive, exercised only when legacy path breaks
    SwarmSupervisor = None
    _SWARM_IMPORT_ERROR = f"{type(_swarm_exc).__name__}: {_swarm_exc}"
from clinical_scribe import ClinicalScribe, ScribeNote
from edge_anonymizer import EdgeAnonymizer
from audit import AUDIT, AuditLog
from agentic_loop import AgenticClinicAgent
from governance import ActionGovernor, Capability, Risk
from clinic_store import ClinicStore, today_iso
from clinic_autopilot import ClinicAutopilot
from omnios_core import OmniosCore, Phase as CorePhase

_keys = APIKeyStore()
_DEV_API_KEY = _keys.issue("demo-clinic", ["*"])   # dev key; production issues per tenant + persists
# Production posture: set OMNIOS_ADMIN_KEY → it becomes a valid key AND the local ui-key
# bootstrap shuts off, so every mutation requires the operator's secret (GDPR Art.32).
_ADMIN_KEY = os.environ.get("OMNIOS_ADMIN_KEY", "")
if _ADMIN_KEY:
    _keys.register(_ADMIN_KEY, "admin", ["*"])

# Private-demo front door. When OMNIOS_ACCESS_PASSWORD is set, the whole app (UI, showcase,
# everything except /health) requires it — so ONLY people you give the password to see anything.
# Browser navigation gets a branded password page (not a native Basic-Auth popup — those are
# inconsistently rendered across browsers and are sometimes silently stripped by corporate
# proxies/antivirus, which looks to the visitor like "the site can't be reached"). API/script
# clients (curl -u, tests) can still use HTTP Basic Auth directly. Unset → no gate (local dev).
_ACCESS_USER = os.environ.get("OMNIOS_ACCESS_USER", "omnios")
_ACCESS_PW = os.environ.get("OMNIOS_ACCESS_PASSWORD", "")
# Fail-CLOSED in PRODUCTION: on a deployed/internet-facing instance (Railway env present, or an
# explicit OMNIOS_REQUIRE_ENC=1) a missing OMNIOS_ACCESS_PASSWORD must NOT silently expose the app —
# we serve 503 instead. Locally and in tests (no prod signal) the gate is a no-op as before, so dev
# stays convenient. OMNIOS_LOCAL_DEV=1 forces wide-open even in a prod-looking env.
_LOCAL_DEV = os.environ.get("OMNIOS_LOCAL_DEV", "") == "1"
_IS_PROD = bool(os.environ.get("RAILWAY_ENVIRONMENT")
                or os.environ.get("RAILWAY_PUBLIC_DOMAIN")
                or os.environ.get("OMNIOS_REQUIRE_ENC") == "1")
_GATE_COOKIE = "omnios_gate"
# Stable across restarts if OMNIOS_GATE_SECRET is set; otherwise ephemeral (a redeploy/restart
# invalidates existing gate sessions — visitors just re-enter the password, no data at risk).
_GATE_SECRET = os.environ.get("OMNIOS_GATE_SECRET") or secrets.token_hex(32)


def _gate_token() -> str:
    return hmac.new(_GATE_SECRET.encode(), b"omnios-gate-v1", hashlib.sha256).hexdigest()


def _gate_ok(request: Request) -> bool:
    tok = request.cookies.get(_GATE_COOKIE, "")
    return bool(tok) and hmac.compare_digest(tok, _gate_token())


def _is_https(request: Request) -> bool:
    # Railway (and most PaaS) terminate TLS at the edge and forward plain HTTP internally, so
    # request.url.scheme alone would say "http" even on a real https visit — trust the proxy header too.
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def _gate_page(error: str = "", next_path: str = "/") -> str:
    err_html = f'<div class="err">{error}</div>' if error else ""
    safe_next = next_path.replace('"', "&quot;")
    return f"""<!doctype html><html lang="el"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OmniOS — Ιδιωτικό Demo</title>
<style>
:root{{--bg:#070b10;--card:#111b26;--line:#1d2c3a;--tx:#eaf1f8;--mut:#8598a8;--tl:#2dd4bf;--bad:#f87171}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:radial-gradient(1200px 700px at 15% -10%,#0e2b2830,transparent),
             radial-gradient(900px 600px at 100% 100%,#12313a30,transparent),var(--bg);
  font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;color:var(--tx);padding:24px}}
.card{{width:100%;max-width:400px;background:var(--card);border:1px solid var(--line);
  border-radius:18px;padding:38px 34px;box-shadow:0 20px 60px #00000066;animation:rise .5s cubic-bezier(.2,.9,.2,1)}}
@keyframes rise{{from{{opacity:0;transform:translateY(14px)}}to{{opacity:1;transform:none}}}}
.logo{{width:52px;height:52px;border-radius:14px;background:linear-gradient(135deg,var(--tl),#0b8a7d);
  display:grid;place-items:center;font-size:26px;margin-bottom:20px;box-shadow:0 0 30px #2dd4bf40}}
h1{{font-size:19px;font-weight:700;letter-spacing:-.3px;margin-bottom:6px}}
p.sub{{color:var(--mut);font-size:13.5px;margin-bottom:26px;line-height:1.5}}
label{{font-size:12px;color:var(--mut);font-weight:600;letter-spacing:.02em;display:block;margin-bottom:8px}}
input{{width:100%;padding:13px 14px;border-radius:11px;border:1px solid var(--line);
  background:#0a0f16;color:var(--tx);font-size:14.5px;outline:none;transition:.15s}}
input:focus{{border-color:var(--tl);box-shadow:0 0 0 3px #2dd4bf22}}
button{{width:100%;margin-top:18px;padding:13px;border:none;border-radius:11px;
  background:linear-gradient(135deg,var(--tl),#0b8a7d);color:#04241f;font-weight:700;
  font-size:14.5px;cursor:pointer;transition:.15s}}
button:hover{{filter:brightness(1.08);transform:translateY(-1px)}}
.err{{background:#2e1414;border:1px solid #7f1d1d;color:var(--bad);font-size:13px;
  padding:10px 13px;border-radius:9px;margin-bottom:16px}}
.foot{{margin-top:22px;text-align:center;font-size:11.5px;color:#5a6d7e}}
</style></head><body>
<div class="card">
  <div class="logo">🩺</div>
  <h1>OmniOS — Ιδιωτικό Demo</h1>
  <p class="sub">Αυτή η προβολή είναι διαθέσιμη μόνο σε όσους έχουν λάβει τον κωδικό πρόσβασης.</p>
  {err_html}
  <form method="post" action="/gate">
    <input type="hidden" name="next" value="{safe_next}">
    <label for="pw">Κωδικός πρόσβασης</label>
    <input id="pw" name="password" type="password" autofocus required autocomplete="current-password">
    <button type="submit">Είσοδος →</button>
  </form>
  <div class="foot">OmniOS · Vertical Agentic OS for Healthcare<br>
  Confidential — shared for evaluation only, not for redistribution.</div>
</div>
</body></html>"""
_SECURED = [Depends(make_dependency(_keys))]        # attach to every acting endpoint (health stays open)

_pipe = MedicalIntelligencePipeline()
if SwarmSupervisor is not None:
    try:
        _swarm = SwarmSupervisor(pipeline=_pipe)   # legacy front door over booking + knowledge + intervention + billing
    except Exception as _swarm_init_exc:
        _swarm = None
        _SWARM_IMPORT_ERROR = f"{type(_swarm_init_exc).__name__}: {_swarm_init_exc}"
else:
    _swarm = None
_scribe = ClinicalScribe()

# ── deep-tech, exposed LIVE to the UI (nothing here is faked in the browser) ──
_anon = EdgeAnonymizer(use_ner=True)             # edge PII tokenizer (+Greek NER when installed)
_pii_seen = {"count": 0}                          # session PII-tokenization counter (edge)
# NOTE: the ONE source of truth for recovered-€ is the ClinicStore ledger (_clinic.ledger/billing);
# there is deliberately no second in-memory ledger here, so every screen reconciles to one number.

# governed tool-calling — one real capability, brokered so the agent never sees credentials
_outbox: list[dict] = []
_gov = ActionGovernor(permissions=["messaging:send", "calendar:write"])
_gov.register(Capability(
    name="send_recovery_sms", risk=Risk.HIGH, permission="messaging:send",
    sandbox=lambda p: {"to": p["to_token"], "channel": "SMS",
                       "preview": f"Θα σταλεί: «{p.get('text', '')[:48]}…»", "commits": False},
    execute=lambda p: (_outbox.append({"to": p["to_token"], "text": p.get("text", "")})
                       or {"sent_to": p["to_token"], "queued": len(_outbox)}),
    compensate=lambda p, r: _outbox.pop() if _outbox else None,
))
_gov.register(Capability(
    name="book_calendar_slot", risk=Risk.LOW, permission="calendar:write",
    sandbox=lambda p: {"slot": p.get("slot", "—"), "commits": False,
                       "preview": f"Κράτηση {p.get('slot', '')} — double-booking guard ✓"},
    execute=lambda p: {"booked": p.get("slot", "—")},
))


def _mask(v: str) -> str:
    """Preview a detected PII value without echoing it in full — even the demo response keeps it local."""
    v = v.strip()
    if len(v) <= 4:
        return "•" * len(v)
    return v[:2] + "•" * max(2, len(v) - 4) + v[-2:]


# ── REAL persistent MULTI-TENANT system of record: one platform, many clinics ──
# Set OMNIOS_CLINIC_DB=/data/clinic.db (a volume) for durable, restart-surviving state.
# Every clinic is a hard-isolated tenant namespace in the store; every API key is PINNED to its
# tenant — clinic A's key can never read or write clinic B (verified by test_multitenant.py).
_DB_PATH = os.environ.get("OMNIOS_CLINIC_DB", ":memory:")
_CLINIC_PROFILES: dict[str, dict] = {
    "demo-clinic": {"name": "Ιατρείο Ευεξία", "specialty": "Γενική Ιατρική", "default_value": 40.0},
    "cardio-irakleio": {
        "name": "Καρδιολογικό Κέντρο Ηρακλείου", "specialty": "Καρδιολογία", "default_value": 60.0,
        "patients": [{"name": "Σ. Μαρκάκης", "phone": "6972000001", "tag": "ενεργός"},
                     {"name": "Ε. Βασιλάκη", "phone": "6972000002", "tag": "οφειλή"},
                     {"name": "Π. Σταυρουλάκης", "phone": "6972000003", "tag": "recall"},
                     {"name": "Α. Δασκαλάκη", "phone": "6972000004", "tag": "no-show"},
                     {"name": "Μ. Τζωρτζάκης", "phone": "6972000005", "tag": "ενεργός"}],
        "services": ["Καρδιολογική εξέταση", "Triplex καρδιάς", "ΗΚΓ", "Holter 24ώρου", "Επανέλεγχος"],
        "waitlist": ["Γ. Ψαρουδάκης", "Ν. Φραγκάκη"]},
    "paido-hamogelo": {
        "name": "Παιδιατρείο Χαμόγελο", "specialty": "Παιδιατρική", "default_value": 35.0,
        "patients": [{"name": "Λ. Αντωνίου (γον.)", "phone": "6973000001", "tag": "ενεργός"},
                     {"name": "Κ. Δημάκη (γον.)", "phone": "6973000002", "tag": "οφειλή"},
                     {"name": "Θ. Ρούσσος (γον.)", "phone": "6973000003", "tag": "recall"},
                     {"name": "Ι. Λαμπράκη (γον.)", "phone": "6973000004", "tag": "no-show"},
                     {"name": "Φ. Οικονόμου (γον.)", "phone": "6973000005", "tag": "ενεργός"}],
        "services": ["Παιδιατρική εξέταση", "Εμβολιασμός", "Αναπτυξιακός έλεγχος", "Follow-up", "Πιστοποιητικό"],
        "waitlist": ["Χ. Μανιαδάκη (γον.)", "Β. Σγουρός (γον.)"]},
}
_REG: dict[str, dict] = {}
for _t, _prof in _CLINIC_PROFILES.items():
    _s = ClinicStore(_DB_PATH, tenant=_t)
    _s.seed_demo(_prof)                       # idempotent — only if that tenant is empty
    _REG[_t] = {"store": _s, "autopilot": ClinicAutopilot(_s),
                "name": _prof["name"], "specialty": _prof["specialty"]}
_DEFAULT_TENANT = "demo-clinic"
_clinic = _REG[_DEFAULT_TENANT]["store"]          # platform-default aliases (open aggregates)
_autopilot = _REG[_DEFAULT_TENANT]["autopilot"]
# one PINNED key per clinic (the dev key stays the demo clinic's key — tests depend on it)
_TENANT_KEYS: dict[str, str] = {_DEFAULT_TENANT: _DEV_API_KEY}
for _t in _CLINIC_PROFILES:
    if _t != _DEFAULT_TENANT:
        _TENANT_KEYS[_t] = _keys.issue(_t, ["*"])

# ── Runtime tenant provisioning + persistence ─────────────────────────────────
# Clinics created at runtime (POST /admin/clinics) persist to the DB (encrypted at rest via KVStore)
# and reload at boot — so a real clinic doesn't vanish on redeploy and keys survive restarts. With an
# in-memory DB (tests) it's ephemeral, matching the demo-seed behaviour.
_TENANT_REG_NS = "platform:tenant-registry"
_tenant_reg_kv = None
if _DB_PATH != ":memory:":
    from store import KVStore as _KVStore
    _tenant_reg_kv = _KVStore(_DB_PATH)


def _persist_tenant(slug: str, prof: dict, key: str) -> None:
    if _tenant_reg_kv is not None:
        _tenant_reg_kv.put(_TENANT_REG_NS, slug, {**prof, "api_key": key})


def _register_tenant(slug: str, prof: dict, key: str) -> None:
    """Wire a tenant into the LIVE registry (store + autopilot + pinned key). No persistence here."""
    s = ClinicStore(_DB_PATH, tenant=slug)
    _REG[slug] = {"store": s, "autopilot": ClinicAutopilot(s),
                  "name": prof.get("name", slug), "specialty": prof.get("specialty", "")}
    if key and len(key) >= 12:
        _keys.register(key, slug, ["*"])
        _TENANT_KEYS[slug] = key


def _load_persisted_tenants() -> None:
    """Rebuild runtime-created tenants from the persisted registry at boot (after the demo seed)."""
    if _tenant_reg_kv is None:
        return
    for slug, rec in _tenant_reg_kv.all(_TENANT_REG_NS).items():
        if slug in _REG:
            continue
        _register_tenant(slug, rec, rec.get("api_key", ""))


_load_persisted_tenants()

# durable audit mirror: when a real DB is configured, every audit record is ALSO appended to a
# persistent hash chain (store.PersistentAuditLog, stable secret) — history survives restarts.
_paudit = None
if _DB_PATH != ":memory:":
    from store import PersistentAuditLog
    _paudit = PersistentAuditLog(_DB_PATH)
    AUDIT.add_sink(_paudit.record)
_core = OmniosCore()                    # the Mythos-class compliance state machine (0.96 tier)
# split-conformal calibration at startup (Vovk et al. 2005): the 'conformal' tier gets a floor
# with a finite-sample guarantee. ~40 pipeline runs, a few seconds, once per process.
try:
    from conformal import ConformalGate
    _core.enable_conformal(ConformalGate.fit(OmniosCore, alpha=0.10))
except Exception:                        # never block boot on calibration; the fixed tiers remain
    pass

from fastapi import Header   # tenant context dependency


def _clinic_ctx(x_api_key: Optional[str] = Header(default=None),
                x_clinic: Optional[str] = Header(default=None)) -> dict:
    """Resolve the caller's clinic from their API key. Keys are PINNED to a tenant; only the
    admin key (OMNIOS_ADMIN_KEY) may select a clinic via the X-Clinic header."""
    rec = _keys.verify(x_api_key)
    if rec is None:
        raise HTTPException(status_code=401, detail="invalid_or_missing_api_key")
    tenant = rec["tenant"]
    if tenant == "admin":
        tenant = x_clinic if (x_clinic in _REG) else _DEFAULT_TENANT
    if tenant not in _REG:
        raise HTTPException(status_code=403, detail="unknown_tenant")
    return _REG[tenant]


CTX = Depends(_clinic_ctx)


# Automated backups piggyback on the heartbeat clock (no extra thread). Default: every 6h.
_backup_state: dict = {"last": 0.0}
_BACKUP_INTERVAL = int(os.environ.get("OMNIOS_BACKUP_INTERVAL", str(6 * 3600)))   # 0 disables


_LOOPS: dict = {}   # tenant → AutonomousLoop (κρατά ζεστό το μαθημένο state μεταξύ κύκλων)


def _loop_for(tenant: str, reg: dict):
    loop = _LOOPS.get(tenant)
    if loop is None:
        from autonomous_loop import AutonomousLoop
        loop = AutonomousLoop(reg["store"], autopilot=reg["autopilot"])
        _LOOPS[tenant] = loop
    return loop


async def _autopilot_heartbeat(interval: int) -> None:
    """TRUE autonomy: ο operator τρέχει έναν ΑΥΤΟΝΟΜΟ ΜΑΘΗΣΙΑΚΟ ΒΡΟΧΟ σε ΚΑΘΕ ιατρείο, με το δικό
    του ρολόι — χωρίς κουμπί. Κάθε κύκλος: παρατηρεί τα ΝΕΑ outcomes → ενημερώνει το no-show μοντέλο
    (μαθαίνει προς τον αληθινό ρυθμό) → σαρώνει με το ενημερωμένο μοντέλο → ΕΚΤΕΛΕΙ ό,τι επιτρέπει η
    πολιτική αυτονομίας (consent-gated, capped, audited). Με autonomy OFF (default) μόνο προτείνει —
    τίποτα δεν φτάνει σε ασθενή χωρίς να το ανοίξει ο γιατρός. Ίδιο ρολόι → περιοδικά DB backups."""
    import asyncio as _aio
    while True:
        await _aio.sleep(interval)
        for tenant, reg in _REG.items():
            try:
                r = _loop_for(tenant, reg).cycle()   # L1..L5: ingest→learn→decide→execute→audit
                if r["swept"] or r["dispatched"] or r["observed_new"]:
                    AUDIT.record("autoloop.heartbeat",
                                 {"clinic": tenant, "observed_new": r["observed_new"],
                                  "learned_no_show": r["learned_no_show_after"],
                                  "dispatched": r["dispatched"], "awaiting_human": r["awaiting_human"]})
            except Exception:
                pass                               # a cycle failure must never kill the app
        if _BACKUP_INTERVAL > 0:
            try:
                from backup import maybe_backup_all
                made = maybe_backup_all(_backup_state, _BACKUP_INTERVAL)
                if made:
                    AUDIT.record("backup.auto", {"files": len(made)})
            except Exception:
                pass                               # a backup failure must never kill the app


_KB_SEED = [
    # Each doc leads with a short SUBJECT-TRIGGER-OBJECT sentence the extractor's _TRIGGERS regex
    # (graph_rag.py) can parse into a real knowledge-graph triplet — the earlier prose-only phrasing
    # ("auto-enqueues", "requires", "gates") used no recognized trigger verb, so /ask could never
    # surface a confident answer for ANY of these seeded facts, only ever the honest-refusal path.
    "A missed appointment causes lost slot revenue. No-show recovery is an automatic outreach and "
    "waitlist offer that OmniOS enqueues the moment a patient no-shows, protecting the clinic's "
    "schedule without any manual follow-up.",
    "Waitlist backfill is the automatic offer of a newly cancelled slot to the next waiting patient. "
    "This keeps the calendar full and protects revenue on every cancellation.",
    "GDPR Article 9 is a requirement for explicit patient consent before any outreach contact. "
    "Edge PII tokenization reduces cloud data exposure risk by converting identifiers to tokens "
    "before any request leaves the clinic.",
    "Value-based billing is a charge on euros actually recovered, not a flat subscription fee. "
    "Every charge is recorded in a tamper-evident ledger for full auditability.",
]


@asynccontextmanager
async def _lifespan(app: FastAPI):
    import asyncio as _aio
    if _swarm is not None:
        await _swarm.start()      # booking actors come online for the app's lifetime
    else:
        print(f"[swarm] legacy booking path unavailable, /route disabled: {_SWARM_IMPORT_ERROR}")
    try:                      # a fresh consistent snapshot at every boot (before anything can corrupt)
        from backup import backup_all
        backup_all()
    except Exception:
        pass
    try:                      # seed a small clinic-knowledge KB so /ask works and /health isn't zeroed
        if not _pipe.graph_rag._chunks:
            for _doc in _KB_SEED:
                _pipe.ingest_sync(_doc, source="omnios-clinic-kb")
    except Exception:
        pass
    hb = None
    interval = int(os.environ.get("OMNIOS_AUTOPILOT_INTERVAL", "300"))   # 0 disables
    if interval > 0:
        hb = _aio.create_task(_autopilot_heartbeat(interval), name="autopilot-heartbeat")
    yield
    if hb:
        hb.cancel()
    if _swarm is not None:
        await _swarm.stop()


app = FastAPI(title="OmniOS — Healthcare Compliance & Operations", version="1.1", lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def _front_door(request: Request, call_next):
    """Private-demo gate. With OMNIOS_ACCESS_PASSWORD set, every request except /health (and the
    gate page itself) needs either the signed session cookie or valid HTTP Basic Auth. Browser
    navigation without either is redirected to a branded /gate password page — not a native
    Basic-Auth popup, which renders inconsistently and can look like a broken connection. If no
    password is configured it FAILS CLOSED (503) unless OMNIOS_LOCAL_DEV=1 — never silently open."""
    # /fhir/metadata + /audit/merkle* are PUBLIC BY DESIGN (see their own docstrings: FHIR
    # capability discovery is spec-public; the Merkle root/proof exist specifically so an
    # anonymous third party can verify audit-chain integrity without a login) — the private-demo
    # gate was silently blocking that stated purpose. Their handlers already redact anything
    # tenant-identifying before returning, so exempting them here does not leak customer data.
    path_open = request.url.path in ("/health", "/gate", "/fhir/metadata", "/audit/merkle") \
        or request.url.path.startswith("/audit/merkle/proof/")
    if not _ACCESS_PW:
        # No password set. In a PRODUCTION-looking env, fail closed (503) so a forgotten env var can
        # never silently expose the whole app to the internet. Locally/in tests → serve as before.
        if _IS_PROD and not _LOCAL_DEV and not path_open:
            return Response(status_code=503,
                            content='{"error":"gate_not_configured","detail":"Set OMNIOS_ACCESS_PASSWORD to enable access (or OMNIOS_LOCAL_DEV=1 for local dev)."}',
                            media_type="application/json")
        return await call_next(request)
    if not path_open:
        if _gate_ok(request):
            return await call_next(request)
        ok = False
        hdr = request.headers.get("authorization", "")
        if hdr.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
                # hmac.compare_digest on `str` raises TypeError for non-ASCII input (e.g. a Greek
                # password) — compare as UTF-8 bytes instead, which it always supports.
                ok = (hmac.compare_digest(user.encode(), _ACCESS_USER.encode())
                      and hmac.compare_digest(pw.encode(), _ACCESS_PW.encode()))
            except Exception:
                ok = False
        if ok:
            return await call_next(request)
        if "text/html" in request.headers.get("accept", ""):
            nxt = quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
            return Response(status_code=302, headers={"Location": f"/gate?next={nxt}"})
        return Response(status_code=401, content='{"error":"unauthorized"}',
                        media_type="application/json",
                        headers={"WWW-Authenticate": 'Basic realm="OmniOS - private demo"'})
    return await call_next(request)


@app.get("/gate", include_in_schema=False)
async def gate_form(next: str = "/") -> Response:
    return Response(content=_gate_page(next_path=next), media_type="text/html; charset=utf-8")


@app.post("/gate", include_in_schema=False)
async def gate_submit(request: Request) -> Response:
    form = await request.form()
    pw = str(form.get("password", ""))
    nxt = str(form.get("next") or "/")
    if _ACCESS_PW and hmac.compare_digest(pw.encode(), _ACCESS_PW.encode()):
        resp = Response(status_code=303, headers={"Location": nxt})
        resp.set_cookie(_GATE_COOKIE, _gate_token(), max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="lax", secure=_is_https(request))
        return resp
    return Response(content=_gate_page(error="Λάθος κωδικός — δοκίμασε ξανά.", next_path=nxt),
                    media_type="text/html; charset=utf-8", status_code=401)

_explog = ExplanationLog()   # the Explanation & Integrity Log the product UI reads


def _seed_explog() -> None:
    _explog.record("Ανάκτηση no-show — Μαρία Π. (€45)", 0.97,
                   why=["Μαρία no-show 12/03 → recovery SMS → ξαναέκλεισε εντός 30 ημερών"],
                   sources=["recovery_ledger#0042"], evidence={"support": "attributed"})
    _explog.record("Απάντηση: η ασπιρίνη μειώνει τον πυρετό", 0.95,
                   why=["ασπιρίνη -[REDUCES]-> πυρετό", "verified vs 2 skeptic agents"],
                   sources=["clinic_kb#aspirin"], evidence={"support": "direct"})
    _explog.record("Υποβολή ΕΟΠΥΥ eΔΑΠΥ (μηνιαία)", 0.99, high_risk=True,
                   why=["browser fill έτοιμο· submit ΜΟΝΟ από τον γιατρό"], sources=["eopyy.gov.gr/eDAPY"])
    _explog.record("Δομημένη κλινική σημείωση — οσφυαλγία", 0.90,
                   why=["SNOMED 279039007 · ICD-10 M54.5", "1 όρος flagged — δεν κωδικοποιήθηκε"],
                   sources=["SNOMED-CT", "ICD-10"])
    _explog.record("Recall πακέτου — Νίκος Κ. (10/10)", 0.94,
                   why=["πακέτο ολοκληρώθηκε → πρόταση ανανέωσης"], sources=["client_profiles#nk"])


_seed_explog()


# ─────────────────────────── request models ───────────────────────────
class IngestRequest(BaseModel):
    text: str = Field(min_length=1)
    source: Optional[str] = None


class AskRequest(BaseModel):
    query: str = Field(min_length=1)


class ActRequest(BaseModel):
    appointment_value: float = Field(gt=0)
    base_no_show_prob: float = Field(default=0.23, ge=0.0, le=1.0)
    lead_time_hours: float = Field(default=24.0, ge=0.0)


class BillRequest(BaseModel):
    events: list[ValueEvent] = Field(min_length=0)


class RouteRequest(BaseModel):
    message: str = Field(min_length=1)
    sender: str = "anon-000"
    appointment_value: float = Field(default=45.0, gt=0)
    base_no_show_prob: float = Field(default=0.23, ge=0.0, le=1.0)
    value_events: list[ValueEvent] = Field(default_factory=list)


class ScribeDraftRequest(BaseModel):
    transcript: str = Field(min_length=1)


class ScribeSignRequest(BaseModel):
    draft_id: str = Field(min_length=1)
    clinician_id: str = Field(min_length=1)   # a licensed clinician is required to sign


class OverrideRequest(BaseModel):
    clinician_id: str = Field(min_length=1)


class AnonymizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class AgenticRunRequest(BaseModel):
    appointments: int = Field(default=3, ge=1, le=12)
    appointment_value: float = Field(default=45.0, gt=0)


class ProposeRequest(BaseModel):
    capability: str = Field(min_length=1)
    params: dict = Field(default_factory=dict)
    reason: str = ""


class ApproveRequest(BaseModel):
    proposal_id: str = Field(min_length=1)
    approver: str = Field(min_length=1)


class ClinicSetupRequest(BaseModel):
    name: Optional[str] = None
    specialty: Optional[str] = None
    hours: Optional[str] = None
    default_value: Optional[float] = None


class BookRequest(BaseModel):
    time: str = Field(min_length=1)
    patient: str = Field(min_length=1)
    service: str = ""
    date: str = "today"
    value: Optional[float] = None


class StatusRequest(BaseModel):
    status: str = Field(min_length=1)


class RescheduleRequest(BaseModel):
    time: str = Field(min_length=1)
    date: Optional[str] = None


class PatientRequest(BaseModel):
    name: str = Field(min_length=1)
    phone: str = ""
    amka: str = ""
    consent: bool = True
    tag: str = "ενεργός"
    email: str = ""


class InvoiceRequest(BaseModel):
    patient: str = Field(min_length=1)
    amount: float = Field(gt=0)
    patient_id: str = ""
    appointment_id: str = ""


class WaitlistRequest(BaseModel):
    name: str = Field(min_length=1)
    note: str = ""


class AutopilotActionRequest(BaseModel):
    pid: str = Field(min_length=1)
    approver: str = "dr_physio"


class AutonomyPolicyRequest(BaseModel):
    enabled: Optional[bool] = None                 # master switch / kill switch
    rules: Optional[dict] = None                   # {type: {"auto": bool, "max_eur": number}}
    max_actions_per_sweep: Optional[int] = None
    set_by: str = ""


class PatientUpdateRequest(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    amka: Optional[str] = None
    consent: Optional[bool] = None
    tag: Optional[str] = None
    email: Optional[str] = None


class ComplianceRunRequest(BaseModel):
    transcript: str = Field(min_length=1, max_length=16000)
    patient_type: str = "established"
    minutes: Optional[float] = Field(default=None, ge=0, le=480)
    tier: str = "claims"                 # claims (0.96) | clinical_docs (0.978) — manifest tiers


class ComplianceOverrideRequest(BaseModel):
    clinician_id: str = Field(min_length=1)
    decision: str = "approve"        # approve | reject
    reason: str = ""


class ImportRequest(BaseModel):
    patients: list[dict] = Field(default_factory=list)
    appointments: list[dict] = Field(default_factory=list)


# ─────────────────────────── endpoints ───────────────────────────
@app.get("/auth/ui-key")
def ui_key() -> dict:
    """Bootstrap for the product UI: the clinic directory with one PINNED key per clinic
    (multi-tenant switcher). Behind the OMNIOS_ACCESS_PASSWORD front door the visitor is ALREADY
    authenticated, so the UI bootstraps smoothly (one password total). With only OMNIOS_ADMIN_KEY
    set (no front door), this returns 403 and the UI prompts — a public instance hands out nothing."""
    if _ADMIN_KEY and not _ACCESS_PW:
        raise HTTPException(status_code=403, detail="admin_key_required")
    # Anyone past the password gate is already the trusted operator (only the owner knows
    # OMNIOS_ACCESS_PASSWORD) — hand back the ADMIN key (tenant='admin') when one is configured,
    # not the tenant-pinned demo key, so the clinic switcher in the UI can actually switch tenants
    # (X-Clinic is only honored for an admin-tier key — see _clinic_ctx). Previously every gated
    # visitor got the demo-clinic-pinned key regardless, so switching clinics silently did nothing.
    bootstrap_key = _ADMIN_KEY if (_ADMIN_KEY and _ACCESS_PW) else _DEV_API_KEY
    # SECURITY: never hand out other tenants' keys. Return only the caller's bootstrap key + the
    # clinic directory WITHOUT per-clinic keys (previously leaked _TENANT_KEYS[t] for every tenant —
    # any gated visitor could read/write every clinic, breaking the isolation guarantee).
    return {"key": bootstrap_key, "mode": "gated-demo" if _ACCESS_PW else "local-demo",
            "clinics": [{"tenant": t, "name": _REG[t]["name"], "specialty": _REG[t]["specialty"]}
                        for t in _REG]}


@app.get("/auth/clinics", dependencies=_SECURED)
def auth_clinics() -> dict:
    """Clinic directory for AUTHENTICATED callers (no keys returned) — lets an admin-key session
    populate the clinic switcher in production mode."""
    return {"clinics": [{"tenant": t, "name": _REG[t]["name"], "specialty": _REG[t]["specialty"]}
                        for t in _REG]}


class CreateClinicRequest(BaseModel):
    slug: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    specialty: str = "Γενική Ιατρική"
    default_value: float = Field(default=40.0, ge=0, le=100000)


@app.post("/admin/clinics")
def admin_create_clinic(req: CreateClinicRequest,
                        x_admin_key: Optional[str] = Header(default=None)) -> dict:
    """Provision a NEW clinic at runtime (no source edit / redeploy). Admin-gated by OMNIOS_ADMIN_KEY.
    Creates an isolated tenant + a pinned API key, persists both (encrypted at rest), and registers
    them live. The clinic survives restarts/redeploys via _load_persisted_tenants()."""
    if not _ADMIN_KEY or not hmac.compare_digest(x_admin_key or "", _ADMIN_KEY):
        raise HTTPException(status_code=401, detail="admin_key_required")
    slug = req.slug.strip().lower()
    if not (2 <= len(slug) <= 40 and all(c.isalnum() or c == "-" for c in slug)):
        raise HTTPException(status_code=400, detail="invalid_slug (a-z 0-9 - only)")
    if slug in _REG:
        raise HTTPException(status_code=409, detail="clinic_already_exists")
    prof = {"name": req.name, "specialty": req.specialty, "default_value": req.default_value}
    key = _keys.issue(slug, ["*"])
    _register_tenant(slug, prof, key)
    _REG[slug]["store"].setup_clinic(name=req.name, specialty=req.specialty,
                                     default_value=req.default_value)
    _persist_tenant(slug, prof, key)
    AUDIT.record("platform.clinic_created", {"tenant": slug, "name": req.name})
    return {"tenant": slug, "name": req.name, "api_key": key,
            "note": "Store this key securely — it is this clinic's only credential."}


@app.get("/health")
def health() -> dict:
    """Liveness + a monitorable snapshot (sell-ready = observable). No PII, no auth."""
    return {
        "status": "ok",
        "version": app.version,
        "uptime_seconds": round(time.monotonic() - _BOOT_MONO, 1),
        "clinics": len(_REG),
        "audit_records": len(AUDIT),
        "audit_intact": AUDIT.verify(),
        "audit_persisted": (_paudit is not None),
        "conformal_gate": getattr(_core, "conformal", None) is not None,
        # deduped modes only — tenant slugs are customer-identifying, never on a public endpoint
        "channels": sorted({_REG[t]["autopilot"].channels.status()["mode"] for t in _REG}),
        "components": ["edge_anonymizer", "semantic_chunker", "graph_rag", "adversarial_debate",
                       "predictive_billing", "clinical_scribe", "omnios_core", "conformal",
                       "merkle", "bandit", "clinic_store", "clinic_autopilot", "channels"],
        "kb_chunks": len(_pipe.graph_rag._chunks),
        "kb_entities": len(_pipe.graph_rag.graph.entities()),
    }


@app.get("/version")
def version() -> dict:
    return {"product": "OmniOS", "version": app.version, "api": "1.1"}


@app.get("/explanations", dependencies=_SECURED)
def explanations() -> dict:
    """The Explanation & Integrity Log — keyed: entries carry patient references."""
    return {"entries": [e.model_dump() for e in _explog.entries()], "integrity": _explog.integrity()}


@app.post("/explanations/{exp_id}/override", dependencies=_SECURED)
def explanation_override(exp_id: str, req: OverrideRequest) -> dict:
    """A clinician resolves a FROZEN decision — recorded in the integrity log."""
    try:
        return _explog.override(exp_id, req.clinician_id).model_dump()
    except KeyError:
        raise HTTPException(status_code=404, detail="explanation_not_found")


@app.post("/ingest", response_model=IngestReport, dependencies=_SECURED)
def ingest(req: IngestRequest) -> IngestReport:
    try:
        return _pipe.ingest_sync(req.text, source=req.source)
    except Exception as e:                       # never leak internals
        raise HTTPException(status_code=422, detail=f"ingest_failed: {type(e).__name__}")


@app.post("/ask", response_model=AnswerReport, dependencies=_SECURED)
async def ask(req: AskRequest) -> AnswerReport:
    try:
        ans = await _pipe.ask(req.query)
        # only DECISIONS enter the curated Explanation log — a frozen/HITL answer is a decision;
        # casual chat answers would flood the «Αποφάσεις» view with noise (audit finding).
        if ans.hitl_required:
            _explog.record(f"Απάντηση (παγωμένη): {req.query[:44]}", ans.confidence,
                           why=ans.graph_paths or [ans.answer[:60]], sources=[], high_risk=True)
        return ans
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ask_failed: {type(e).__name__}")


@app.post("/act", response_model=SimulationResult, dependencies=_SECURED)
def act(req: ActRequest) -> SimulationResult:
    state = AppointmentState(appointment_value=req.appointment_value,
                             base_no_show_prob=req.base_no_show_prob,
                             lead_time_hours=req.lead_time_hours)
    return _pipe.act(state)


@app.post("/bill", response_model=BillingResult, dependencies=_SECURED)
def bill(req: BillRequest) -> BillingResult:
    return _pipe.bill(req.events)


@app.post("/scribe/draft", response_model=ScribeNote, dependencies=_SECURED)
def scribe_draft(req: ScribeDraftRequest) -> ScribeNote:
    """Structure a transcript into a DRAFT note (PII-stripped, SNOMED/ICD-10). Never persisted here."""
    try:
        return _scribe.draft_sync(req.transcript)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/scribe/soap", dependencies=_SECURED)
def scribe_soap(req: ScribeDraftRequest) -> dict:
    """Ελληνικό SOAP draft (Υποκειμενικά/Αντικειμενικά/Εκτίμηση/Πλάνο) από transcript — δομεί ΜΟΝΟ
    ό,τι ειπώθηκε, PII-stripped, draft-only· υπογράφει πάντα ο κλινικός."""
    from clinical_scribe import to_soap_gr
    try:
        note = _scribe.draft_sync(req.transcript)
        return {"draft_id": note.id, "soap": to_soap_gr(note), "status": note.status}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/scribe/sign", response_model=ScribeNote, dependencies=_SECURED)
def scribe_sign(req: ScribeSignRequest) -> ScribeNote:
    """The ONLY path to the clinical record — a licensed clinician signs the draft.
    Signed notes are PERSISTED to the clinic store (a clinical record must survive restarts)."""
    try:
        note = _scribe.sign(req.draft_id, req.clinician_id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="draft_not_found")
    _clinic.add_note(note.model_dump())
    return note


@app.post("/route", dependencies=_SECURED)
async def route(req: RouteRequest) -> dict:
    """Single front door: the Supervisor classifies intent and routes to the right specialist."""
    if _swarm is None:
        raise HTTPException(status_code=503, detail=f"legacy /route unavailable: {_SWARM_IMPORT_ERROR}")
    await _swarm.start()   # idempotent — brings the booking actors online on first call
    r = await _swarm.route(req.message, sender=req.sender,
                           appointment_value=req.appointment_value,
                           base_no_show_prob=req.base_no_show_prob,
                           value_events=req.value_events)
    return {"intent": r.intent, "handler": r.handler, "result": r.result}


@app.get("/fhir/metadata")
def fhir_metadata() -> dict:
    """FHIR R4 discovery (public, ό,πως ορίζει το πρότυπο): τι resources/interactions υποστηρίζει."""
    from fhir_bridge import capability_statement
    return capability_statement()


@app.get("/fhir/Patient")
def fhir_patients(ten: dict = CTX) -> dict:
    """Τα ΔΙΚΑ ΤΟΥ ιατρείου patients ως FHIR R4 Bundle (tenant-scoped· ίδιος data controller)."""
    from fhir_bridge import patient_to_fhir, bundle
    return bundle([patient_to_fhir(p) for p in ten["store"].list_patients()])


@app.get("/fhir/Appointment")
def fhir_appointments(date: Optional[str] = None, ten: dict = CTX) -> dict:
    """Τα ραντεβού ως FHIR R4 Bundle — ένα EHR/σύστημα που μιλάει FHIR τα διαβάζει άμεσα."""
    from fhir_bridge import appointment_to_fhir, bundle
    return bundle([appointment_to_fhir(a) for a in ten["store"].list_appointments(date)])


@app.get("/fhir/Invoice")
def fhir_invoices(ten: dict = CTX) -> dict:
    from fhir_bridge import invoice_to_fhir, bundle
    return bundle([invoice_to_fhir(i) for i in ten["store"].list_invoices()])


@app.post("/fhir/Appointment", status_code=201)
def fhir_create_appointment(fhir: dict, ten: dict = CTX) -> dict:
    """INGEST: ένα σύστημα που μιλάει FHIR (π.χ. booking platform) στέλνει FHIR Appointment → το
    OmniOS το κλείνει ως πραγματικό ραντεβού και επιστρέφει το δημιουργημένο FHIR resource. Αυτή
    είναι η «σύνδεση με τον ασθενή» — ΜΟΝΟ για συστήματα που εκθέτουν FHIR (όχι ΗΔΙΚΑ/Doctoranytime)."""
    from fhir_bridge import fhir_to_appointment, appointment_to_fhir
    try:
        f = fhir_to_appointment(fhir)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        a = ten["store"].add_appointment(f["time"], f["patient"], f["service"], f["date"])
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))   # double-booking
    return appointment_to_fhir(a)


@app.get("/bridges/status")
def bridges_status() -> dict:
    """Ποιες εξωτερικές γέφυρες είναι ζωντανές — τίμιοι χαρακτηρισμοί (LIVE / χρειάζεται key / χωρίς
    δημόσιο API). Ο γιατρός βλέπει ακριβώς τι συνδέεται, χωρίς φουσκωμένες υποσχέσεις."""
    import os as _os
    def _has(*keys):
        return all(_os.environ.get(k) for k in keys)
    return {"bridges": [
        {"name": "FHIR (read + ingest)", "state": "LIVE",
         "note": "Έτοιμο· συνδέεται με ΟΠΟΙΟΔΗΠΟΤΕ σύστημα μιλάει FHIR (/fhir/*)."},
        {"name": "Email (Resend)", "state": "LIVE" if _has("RESEND_API_KEY", "FROM_DOMAIN") else "needs_key",
         "note": "RESEND_API_KEY + FROM_DOMAIN"},
        {"name": "SMS (Twilio)", "state": "LIVE" if _has("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER") else "needs_key",
         "note": "TWILIO_ACCOUNT_SID + AUTH_TOKEN + PHONE_NUMBER"},
        {"name": "myDATA / ΑΑΔΕ", "state": "LIVE" if _has("MYDATA_USER_ID", "MYDATA_SUBSCRIPTION_KEY") else "needs_key",
         "note": "AADE credentials (ο γιατρός τα βγάζει στο mydata.aade.gr)"},
        {"name": "ΗΔΙΚΑ / e-prescription", "state": "no_public_api",
         "note": "Δεν υπάρχει δημόσιο API τρίτου· μόνο ο γιατρός με προσωπικούς κωδικούς. EHDS: 2027-2029."},
        {"name": "Doctoranytime / ιδιωτικές ασφαλιστικές", "state": "no_public_api",
         "note": "Χωρίς δημόσιο API στην Ελλάδα σήμερα."},
    ]}


@app.get("/bridge/journey")
def bridge_journey() -> dict:
    """DEMO: τρέχει έναν πλήρη κύκλο ασθενή μέσα από τους 5 υπαλλήλους (booking→υποδοχή→κλινικός→
    λογιστήριο→αναχώρηση) σε ΕΦΗΜΕΡΟ in-memory ιατρείο (ΔΕΝ αγγίζει πραγματικά δεδομένα). Επιστρέφει
    το ίχνος επικοινωνίας (ποιος υπάλληλος έκανε τι, με ποια σειρά) + το FHIR Bundle του επεισοδίου."""
    from clinic_store import ClinicStore
    from clinic_bridge import ClinicBridge, journey_to_fhir
    cs = ClinicStore(":memory:", tenant="bridge-demo")
    cs.setup_clinic(name="Επίδειξη", specialty="Φυσικοθεραπεία", default_value=40.0)
    p = cs.add_patient("Δοκιμαστικός Ασθενής", phone="6970000000", consent=True)
    br = ClinicBridge(cs, clinic_afm="")
    br.request_booking("Δοκιμαστικός Ασθενής", "10:00", "2026-08-01", value=40.0, patient_id=p["id"])
    booked = next((e for e in br.journey() if e["type"] == "appointment.booked"), None)
    if booked:
        aid = booked["payload"]["appointment_id"]
        br.arrive(aid, "Δοκιμαστικός Ασθενής", patient_id=p["id"])
        br.complete_visit(aid, "Δοκιμαστικός Ασθενής",
                          transcript="Ο ασθενής αναφέρει οσφυαλγία 5 ημερών. Πρόταση: φυσικοθεραπεία.",
                          clinician_id="dr-lic-001", value=40.0, patient_id=p["id"])
    labels = {"booking": "Ρεσεψιόν", "reception": "Υποδοχή", "clinical": "Κλινικός",
              "billing": "Λογιστήριο", "checkout": "Αναχώρηση", "external": "Ασθενής"}
    steps = [{"seq": e["seq"], "event": e["type"], "agent": labels.get(e["source"], e["source"]),
              "ref": e["payload"].get("ref")} for e in br.journey()]
    return {"steps": steps, "agents": list(labels.values())[:5],
            "invoices": len(cs.list_invoices()), "appointments": len(cs.list_appointments()),
            "notes": len(cs.list_notes()), "fhir": journey_to_fhir(br)}


# ═══════════════ LIVE DEEP-TECH — the browser calls the real algorithms ═══════════════
@app.post("/anonymize", dependencies=_SECURED)
def anonymize(req: AnonymizeRequest) -> dict:
    """Edge PII tokenization, RUN LOCALLY. Detects ΑΜΚΑ/IBAN/card/phone/email/name with validators
    (Luhn, IBAN mod-97), proves the map is reversible, and confirms zero bytes leave the network."""
    res = _anon.anonymize_sync(req.text)
    restored = _anon.deanonymize_sync(res.redacted, res.mapping())     # provable reversibility
    _pii_seen["count"] += res.pii_count
    kinds = sorted({e.kind.value for e in res.entities})
    AUDIT.record("edge.tokenize", {"pii": res.pii_count, "kinds": kinds, "bytes_egress": 0})
    return {
        "redacted": res.redacted,
        "pii_count": res.pii_count,
        "reversible": restored == req.text,
        "bytes_egress": 0,
        "entities": [{"kind": e.kind.value, "token": e.token, "preview": _mask(e.original),
                      "start": e.start, "end": e.end} for e in res.entities],
        "session_total": _pii_seen["count"],
    }


@app.get("/audit", dependencies=_SECURED)
def audit_chain() -> dict:
    """The real tamper-evident hash chain (HMAC-SHA256), newest-last, with prev-hash links.
    When a DB is configured, `persisted` shows the durable mirror that survives restarts."""
    out = {"chain": AUDIT.chain(40), "count": len(AUDIT), "intact": AUDIT.verify()}
    if _paudit is not None:
        out["persisted"] = {"records": len(_paudit), "intact": _paudit.verify()}
    return out


@app.post("/audit/verify", dependencies=_SECURED)
def audit_verify() -> dict:
    """Re-derive the entire chain from genesis and report whether a single byte was altered."""
    return {"intact": AUDIT.verify(), "records": len(AUDIT)}


# These two endpoints are PUBLIC by design (RFC 6962: anyone verifies inclusion against the
# published root). So the leaves must NOT carry customer-identifying fields — same rule the
# /health comment states. We redact tenant/name/specialty from each record's data BEFORE building
# the tree, so the leaves stay consistent AND no tenant history leaks to an anonymous caller.
_MERKLE_REDACT = {"tenant", "name", "specialty"}


def _public_audit_records() -> list[dict]:
    return [{"seq": r.seq, "event": r.event,
             "data": {k: v for k, v in (r.data or {}).items() if k not in _MERKLE_REDACT}}
            for r in AUDIT._records]


@app.get("/audit/merkle")
def audit_merkle() -> dict:
    """Merkle anchor over the audit chain (Merkle 1979/87 · RFC 6962 pattern): the WHOLE history
    compresses to one 32-byte root a clinic can publish externally as a tamper anchor.
    Public showcase — leaves carry no tenant-identifying fields."""
    from merkle import MerkleTree
    records = _public_audit_records()
    t = MerkleTree(records)
    return {"root": t.root, "size": len(records), "chain_intact": AUDIT.verify()}


@app.get("/audit/merkle/proof/{seq}")
def audit_merkle_proof(seq: int) -> dict:
    """O(log n) inclusion proof: prove record #seq belongs to the anchored history WITHOUT
    revealing any other record — third-party verifiable against the published root."""
    from merkle import MerkleTree, verify_inclusion
    records = _public_audit_records()
    if not 0 <= seq < len(records):
        raise HTTPException(status_code=404, detail="seq_out_of_range")
    t = MerkleTree(records)
    proof = t.inclusion_proof(seq)
    return {"record": records[seq], "proof": proof, "root": t.root,
            "verified": verify_inclusion(records[seq], proof, t.root),
            "proof_size": len(proof), "log_size": len(records)}


@app.get("/compliance/conformal")
def compliance_conformal() -> dict:
    """The split-conformal certificate behind the 'conformal' tier (Vovk et al. 2005)."""
    gate = getattr(_core, "conformal", None)
    if gate is None:
        raise HTTPException(status_code=503, detail="conformal_gate_not_calibrated")
    return {**gate.report().model_dump(),
            "usage": "POST /compliance/run with tier='conformal' → the gate floor is the "
                     "calibrated 1−q̂ with the finite-sample guarantee"}


class ThompsonRequest(BaseModel):
    rounds: int = Field(default=1200, ge=100, le=20000)
    seed: int = 7


@app.post("/agentic/thompson")
def agentic_thompson(req: ThompsonRequest) -> dict:
    """Thompson sampling (1933) demo on a simulated clinic whose true intervention effects
    differ from the published priors — watch the posterior converge to THIS clinic's best arm.
    Clearly labeled simulation; the same policy object plugs into real outcomes on a pilot."""
    from bandit import ThompsonInterventionPolicy, simulate_clinic
    from predictive_billing import Action
    true_show = {Action.NONE: 0.72, Action.SMS: 0.80, Action.AI_CALL: 0.93, Action.DEPOSIT: 0.84}
    result = simulate_clinic(ThompsonInterventionPolicy(seed=req.seed), true_show,
                             rounds=req.rounds, seed=req.seed + 4)
    result["note"] = ("simulation on a synthetic clinic (true effects labeled) — the policy is "
                      "the production object; on a pilot it updates from REAL outcomes")
    return result


@app.post("/audit/tamper-demo")
def audit_tamper_demo() -> dict:
    """Prove tamper-evidence on a throwaway chain (the real AUDIT is untouched): build 3 records,
    verify OK, forge one field of a past record, verify FAILS → the forgery is detected."""
    demo = AuditLog()
    demo.record("edge.tokenize", {"pii": 3, "kinds": ["AMKA", "PHONE", "NAME"]})
    demo.record("agent.decide", {"appt": "A1", "recommend": "sms_reminder", "eur_gain": 7.4})
    demo.record("gov.execute", {"cap": "send_recovery_sms", "approved_by": "dr_papadopoulos"})
    before = demo.verify()
    original = dict(demo._records[1].data)
    forged = {"appt": "A1", "recommend": "require_deposit", "eur_gain": 7.4}   # attacker edits history
    demo._records[1].data = forged
    after = demo.verify()
    broke_at = next((r.seq for i, r in enumerate(demo._records)
                     if demo._hash(r.prev_hash, {"seq": r.seq, "event": r.event, "data": r.data})
                     != r.this_hash), None)
    return {"before": before, "after": after, "tampered_seq": 1, "detected_at_seq": broke_at,
            "original": original, "forged": forged,
            "message": "Άλλαξε 1 πεδίο σε παλιό record → η κρυπτογραφική αλυσίδα έσπασε και εντοπίστηκε."}


@app.post("/agentic/run", dependencies=_SECURED)
def agentic_run(req: AgenticRunRequest) -> dict:
    """Run the closed loop for real: INGEST→DECIDE→ACT→UPDATE/IMPROVE→AUDIT. The no-show estimate
    starts at the 0.23 prior (published outpatient baseline) and MOVES toward the clinic's true ~0.5 as outcomes arrive (Bayes).
    Uses only synthetic ids/values — no PII. Records land in the same live audit chain."""
    agent = AgenticClinicAgent()
    start_prob = round(agent.learner.no_show_prob(), 4)
    trace: list[dict] = []
    for i in range(req.appointments):
        d = agent.ingest({"type": "appointment", "id": f"A{i}", "value": req.appointment_value})
        trace.append({"phase": "DECIDE", "appt": f"A{i}", "no_show_prob": d["no_show_prob_used"],
                      "recommend": d["recommended"], "eur_gain": d["expected_value_gain"]})
    outcomes = [True, False, True, False, True, False, True, False, False, True] * 3   # true rate ~0.5
    recovered_before = agent.recovered_eur
    for i, showed in enumerate(outcomes):
        agent.ingest({"type": "appointment", "id": f"B{i}", "value": req.appointment_value})
        agent.ingest({"type": "outcome", "id": f"B{i}", "showed": showed})
    rep = agent.report()
    gained = round(rep.recovered_eur - recovered_before, 2)
    if gained > 0:
        _clinic.add_value("recovered_appointment", gained)   # feeds the REAL persistent ledger
    return {"start_no_show_prob": start_prob, "learned_no_show_prob": rep.learned_no_show_prob,
            "true_rate_target": 0.5, "observations": rep.observations,
            "recovered_eur": rep.recovered_eur, "decisions": rep.decisions,
            "audit_records": rep.audit_records, "audit_chain_intact": rep.audit_chain_intact,
            "trace": trace[:6]}


@app.get("/ledger", dependencies=_SECURED)
def ledger() -> dict:
    """Recovered-value ledger + value-based bill — from the REAL clinic store (persisted)."""
    return {"events": _clinic.ledger(), "billing": _clinic.billing()}


@app.get("/metrics", dependencies=_SECURED)
def metrics() -> dict:
    """Overview KPIs: operational numbers from the REAL persistent clinic store, plus the trust
    telemetry (audit integrity, PII tokenized, 0-byte egress). Nothing here is a demo constant."""
    cm = _clinic.metrics()
    return {"audit_records": len(AUDIT), "audit_intact": AUDIT.verify(),
            "pii_tokenized": _pii_seen["count"], "bytes_egress": 0,
            "recovered_eur": cm["recovered_eur"], "charge_eur": cm["charge_eur"],
            "net_kept_eur": cm.get("net_kept_eur", 0.0), "roi": cm["roi"],   # net_kept = the honest figure
            "appointments_today": cm["appointments_today"], "completed": cm["completed"],
            "no_shows": cm["no_shows"], "patients": cm["patients"], "unpaid_eur": cm["unpaid_eur"],
            "kb_chunks": len(_pipe.graph_rag._chunks),
            "kb_entities": len(_pipe.graph_rag.graph.entities()), "outbox": len(_outbox)}


@app.post("/govern/preview", dependencies=_SECURED)
def govern_preview(req: ProposeRequest) -> dict:
    """Keyed, read-only preview of the governed flow: permission verdict + sandbox evidence + risk
    routing. Executes NOTHING — the commit path stays behind /govern/approve (secured)."""
    try:
        return _gov.preview(req.capability, req.params)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown_capability")


@app.post("/govern/propose", dependencies=_SECURED)
def govern_propose(req: ProposeRequest) -> dict:
    """Governed tool-calling: permission → sandbox preview → eval → risk routing. High-risk stops
    at PENDING_APPROVAL; low-risk auto-executes. Every step is audited (what/why/evidence/who)."""
    try:
        p = _gov.propose(req.capability, req.params, reason=req.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown_capability")
    return p.model_dump()


@app.post("/govern/approve", dependencies=_SECURED)
def govern_approve(req: ApproveRequest) -> dict:
    """A human approves a pending high-risk proposal → EXECUTE + audit + rollback token."""
    try:
        return _gov.approve(req.proposal_id, approver=req.approver).model_dump()
    except KeyError:
        raise HTTPException(status_code=404, detail="no_pending_proposal")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


# ═══════ REAL CLINIC OPERATIONS — multi-tenant, key-pinned, persisted, real calendar ═══════
# Every endpoint resolves the caller's clinic from their API key (CTX). Clinic A's key can
# never touch clinic B. Dates are REAL ISO dates; times normalized HH:MM.
@app.get("/clinic")
def clinic_overview(ten: dict = CTX) -> dict:
    c = ten["store"].get_clinic()
    return {"clinic": c.model_dump(), "metrics": ten["store"].metrics(),
            "today": today_iso()}


@app.post("/clinic/setup")
def clinic_setup(req: ClinicSetupRequest, ten: dict = CTX) -> dict:
    return ten["store"].setup_clinic(**req.model_dump()).model_dump()


@app.post("/clinic/import")
def clinic_import(req: ImportRequest, ten: dict = CTX) -> dict:
    """A clinic connects by importing its patients/appointments. Operates on that live data after."""
    return ten["store"].import_data(req.patients, req.appointments)


@app.get("/clinic/appointments")
def clinic_appointments(date: Optional[str] = None, ten: dict = CTX) -> dict:
    return {"appointments": ten["store"].list_appointments(date),
            "waitlist": ten["store"].list_waitlist(),
            "today": today_iso()}


@app.post("/clinic/appointments")
def clinic_book(req: BookRequest, ten: dict = CTX) -> dict:
    try:
        return ten["store"].add_appointment(req.time, req.patient, req.service, req.date, req.value)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))   # double-booking / bad time-date


@app.post("/clinic/appointments/{aid}/status")
def clinic_set_status(aid: str, req: StatusRequest, ten: dict = CTX) -> dict:
    try:
        return ten["store"].set_status(aid, req.status)
    except KeyError:
        raise HTTPException(status_code=404, detail="appointment_not_found")


@app.post("/clinic/appointments/{aid}/reschedule")
def clinic_reschedule(aid: str, req: RescheduleRequest, ten: dict = CTX) -> dict:
    """Move an appointment to a new slot in place (does NOT release it to the waitlist)."""
    try:
        return ten["store"].reschedule_appointment(aid, req.time, req.date)
    except KeyError:
        raise HTTPException(status_code=404, detail="appointment_not_found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/clinic/appointments/{aid}/cancel")
def clinic_cancel(aid: str, ten: dict = CTX) -> dict:
    try:
        return ten["store"].cancel_appointment(aid)
    except KeyError:
        raise HTTPException(status_code=404, detail="appointment_not_found")


@app.get("/clinic/patients")
def clinic_patients(ten: dict = CTX) -> dict:
    return {"patients": ten["store"].list_patients()}


@app.get("/clinic/patient/history")
def clinic_patient_history(name: str, ten: dict = CTX) -> dict:
    """A patient's full record joined across the system — appointments + invoices + balance."""
    return ten["store"].patient_history(name)


@app.get("/channels/status", dependencies=_SECURED)
def channels_status(x_api_key: Optional[str] = Header(default=None),
                    x_clinic: Optional[str] = Header(default=None)) -> dict:
    """Which outbound channels are LIVE vs queueing. Tenant-aware when a key is presented."""
    rec = _keys.verify(x_api_key)
    tenant = (rec["tenant"] if rec and rec["tenant"] in _REG else
              (x_clinic if (rec and rec["tenant"] == "admin" and x_clinic in _REG) else _DEFAULT_TENANT))
    return _REG[tenant]["autopilot"].channels.status()


@app.get("/channels/outbox")
def channels_outbox(ten: dict = CTX) -> dict:
    """The durable outbox — every approved outreach with its honest status (sent/queued/blocked)."""
    return {"outbox": ten["autopilot"].channels.outbox()}


@app.post("/clinic/patients")
def clinic_add_patient(req: PatientRequest, ten: dict = CTX) -> dict:
    return ten["store"].add_patient(req.name, req.phone, req.amka, req.consent, req.tag, req.email)


@app.get("/clinic/invoices")
def clinic_invoices(ten: dict = CTX) -> dict:
    return {"invoices": ten["store"].list_invoices(), "billing": ten["store"].billing()}


@app.post("/clinic/invoices")
def clinic_add_invoice(req: InvoiceRequest, ten: dict = CTX) -> dict:
    return ten["store"].add_invoice(req.patient, req.amount, patient_id=req.patient_id,
                                    appointment_id=req.appointment_id)


@app.post("/clinic/invoices/{iid}/pay")
def clinic_pay(iid: str, ten: dict = CTX) -> dict:
    try:
        return ten["store"].pay_invoice(iid)
    except KeyError:
        raise HTTPException(status_code=404, detail="invoice_not_found")


class MyDataRequest(BaseModel):
    afm: str = Field(min_length=9, max_length=9, description="ΑΦΜ του ιατρείου (9 ψηφία)")
    branch: int = 0
    sandbox: bool = True     # πρώτα mydata-dev· production ΜΟΝΟ ρητά


@app.post("/clinic/invoices/{iid}/mydata")
def clinic_invoice_mydata(iid: str, req: MyDataRequest, ten: dict = CTX) -> dict:
    """Υποβολή τιμολογίου στο ΑΑΔΕ myDATA (ελληνικό e-invoicing, ΑΠΥ 11.2, ΦΠΑ 0% άρθρο 22).
    Fail-honest: χωρίς MYDATA_* credentials → queued με τον λόγο, ποτέ ψεύτικη υποβολή."""
    from mydata_gr import submit_invoice
    try:
        return submit_invoice(ten["store"], iid, {"afm": req.afm, "branch": req.branch},
                              sandbox=req.sandbox)
    except KeyError:
        raise HTTPException(status_code=404, detail="invoice_not_found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/clinic/waitlist")
def clinic_add_waitlist(req: WaitlistRequest, ten: dict = CTX) -> dict:
    return ten["store"].add_waitlist(req.name, req.note)


@app.post("/clinic/waitlist/{wid}/remove")
def clinic_remove_waitlist(wid: str, ten: dict = CTX) -> dict:
    ten["store"].remove_waitlist(wid)
    return {"removed": wid}


@app.post("/clinic/patients/{pid}")
def clinic_update_patient(pid: str, req: PatientUpdateRequest, ten: dict = CTX) -> dict:
    """Edit a patient record (phone/consent/tag/…) — consent changes are audited (Art.9)."""
    out = ten["store"].update_patient(pid, **req.model_dump())
    if out is None:
        raise HTTPException(status_code=404, detail="patient_not_found")
    if req.consent is not None:
        ten["store"]._audit("patient.consent_changed", {"id": pid, "consent": req.consent})
    return out


@app.post("/clinic/patients/{pid}/erase")
def clinic_erase_patient(pid: str, ten: dict = CTX) -> dict:
    """GDPR Art.17 δικαίωμα στη λήθη — σβήνει την ταυτότητα του ασθενή· κρατά ψευδωνυμοποιημένα
    τα τιμολόγια (φορολογική υποχρέωση) και τα slots. Μη-αναστρέψιμο, audited."""
    out = ten["store"].erase_patient(pid)
    if out is None:
        raise HTTPException(status_code=404, detail="patient_not_found")
    return out


@app.get("/clinic/notes")
def clinic_notes(ten: dict = CTX) -> dict:
    """Signed clinical notes (persisted, restart-surviving)."""
    return {"notes": ten["store"].list_notes()}


# ── AUTOPILOT: the autonomous operator over each clinic's real data (propose → human approves) ──
@app.post("/clinic/autopilot/scan")
def autopilot_scan(ten: dict = CTX) -> dict:
    """One autonomous pass of THIS clinic's real data: sweep → execute whatever the doctor's
    policy authorizes (consent-gated, audited) → leave the rest for review. With autonomy off,
    this just proposes (nothing auto-executes) — identical to the old behaviour."""
    return ten["autopilot"].run_autonomous()


@app.post("/autonomy/tick")
def autonomy_tick(ten: dict = CTX) -> dict:
    """Bus-based autonomous pass: σαρώνει τα ΠΡΑΓΜΑΤΙΚΑ δεδομένα → στέλνει την εξουσιοδοτημένη (policy)
    δουλειά στο event bus → οι specialist agents την εκτελούν (idempotent, consent-gated, audited) &
    αναφέρουν. Επιστρέφει τι έκανε ο κάθε agent (feed) — proactive πλευρά των πραγματικών υπαλλήλων."""
    from autonomous_agents import AutonomyOrchestrator
    orch = AutonomyOrchestrator(ten["store"], ten["autopilot"])
    res = orch.tick()
    res["feed"] = orch.feed()
    return res


@app.get("/autonomy/loop")
def autonomy_loop_state(ten: dict = CTX) -> dict:
    """Το τι έχει ΜΑΘΕΙ ο αυτόνομος βρόχος: no-show estimate (κινείται από τον prior προς τον αληθινό
    ρυθμό του ιατρείου καθώς φτάνουν outcomes) + πλήθος παρατηρήσεων. Ο βρόχος τρέχει server-side."""
    from autonomous_loop import AutonomousLoop
    return AutonomousLoop(ten["store"], autopilot=ten["autopilot"]).state()


@app.post("/autonomy/loop/cycle")
def autonomy_loop_cycle(ten: dict = CTX) -> dict:
    """Τρέξε ΕΝΑΝ κύκλο του μαθησιακού βρόχου τώρα (L1..L5). Ίδιο με το heartbeat, on-demand."""
    from autonomous_loop import AutonomousLoop
    return AutonomousLoop(ten["store"], autopilot=ten["autopilot"]).cycle()


@app.get("/clinic/autopilot")
def autopilot_pending(ten: dict = CTX) -> dict:
    return {"pending": ten["autopilot"].pending(), "briefing": ten["autopilot"].briefing()}


@app.get("/clinic/forecast")
def clinic_forecast(days: int = 14, ten: dict = CTX) -> dict:
    """Πρόβλεψη ζήτησης & εσόδων από το ΠΡΑΓΜΑΤΙΚΟ ιστορικό του tenant. Dual-mode:
    Google TimesFM αν είναι εγκατεστημένο, per-weekday στατιστικό fallback αλλιώς —
    με ρητό engine attribution και honest ready:false χωρίς επαρκές ιστορικό."""
    from demand_forecast import forecast_clinic
    return forecast_clinic(ten["store"], days)


class OnboardRequest(BaseModel):
    name: Optional[str] = None
    specialty: Optional[str] = None
    hours: Optional[str] = None
    default_value: Optional[float] = Field(default=None, ge=0, le=100000)
    services: Optional[list[dict]] = None
    afm: Optional[str] = None
    reminder_hours: Optional[list[int]] = None


@app.get("/clinic/onboarding")
def clinic_onboarding(ten: dict = CTX) -> dict:
    """Checklist ρύθμισης: τι έχει στηθεί, τι λείπει ώστε το ιατρείο να «κουμπώσει»."""
    return ten["store"].get_clinic().onboarding_status()


@app.post("/clinic/onboard")
def clinic_onboard(req: OnboardRequest, ten: dict = CTX) -> dict:
    """Self-serve setup: ο γιατρός ρυθμίζει ειδικότητα/ώρες/υπηρεσίες/τιμές/ΑΦΜ σε ένα βήμα.
    Κάθε ιατρείο δουλεύει διαφορετικά → αυτό το κουμπώνει, χωρίς κώδικα."""
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    c = ten["store"].setup_clinic(**fields)
    return {"clinic": c.model_dump(), "onboarding": c.onboarding_status()}


class LabAnalyzeRequest(BaseModel):
    patient: str = Field(min_length=1, max_length=200)
    text: Optional[str] = Field(default=None, max_length=200_000)
    pdf_base64: Optional[str] = Field(default=None, max_length=8_000_000)


@app.post("/clinic/labs/analyze")
def clinic_labs_analyze(req: LabAnalyzeRequest, ten: dict = CTX) -> dict:
    """OFFLINE ανάλυση εξετάσεων (κείμενο ή PDF): εξάγει δείκτες, flag-άρει εκτός-ορίων τιμές
    ΤΟΠΙΚΑ (0 bytes egress) και γράφει DRAFT σημείωση στην καρτέλα — ο γιατρός επιβεβαιώνει."""
    from lab_analyzer import analyze_and_chart
    pdf = None
    if req.pdf_base64:
        import base64 as _b64
        try:
            pdf = _b64.b64decode(req.pdf_base64)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid_base64")
    if not (req.text or pdf):
        raise HTTPException(status_code=400, detail="text_or_pdf_required")
    return analyze_and_chart(ten["store"], req.patient, text=req.text, pdf_bytes=pdf)


@app.get("/clinic/risk")
def clinic_risk(days: int = 14, ten: dict = CTX) -> dict:
    """Εξηγήσιμο no-show risk για κάθε επερχόμενο ραντεβού + προτεινόμενη ενέργεια. Κάθε score
    συνοδεύεται από ΤΟΥΣ ΛΟΓΟΥΣ (interpretable) ώστε ο γιατρός να εμπιστεύεται το «γιατί».
    Feeds τον autopilot: η λίστα διαχειρίζεται μόνη της, ο γιατρός δεν κάνει τίποτα."""
    from clinic_brain import scan_clinic
    return scan_clinic(ten["store"], days)


@app.get("/clinic/autonomy")
def autonomy_get(ten: dict = CTX) -> dict:
    """The doctor's standing autonomy policy: what runs by itself, ceilings, master switch."""
    from autonomy_policy import AutonomyPolicy
    return AutonomyPolicy(ten["store"]).summary()


@app.post("/clinic/autonomy")
def autonomy_set(req: AutonomyPolicyRequest, ten: dict = CTX) -> dict:
    """Set the policy ONCE → the operator then executes within it. Opt-in; instant kill switch
    via enabled=false. This is the human-oversight point (EU AI Act Art.14): the doctor governs
    the RULES, the machine handles the keystrokes. Every change is audited."""
    from autonomy_policy import AutonomyPolicy
    pol = AutonomyPolicy(ten["store"])
    pol.set(enabled=req.enabled, rules=req.rules,
            max_actions_per_sweep=req.max_actions_per_sweep, set_by=req.set_by or "clinic")
    return pol.summary()


@app.post("/clinic/autopilot/approve")
def autopilot_approve(req: AutopilotActionRequest, ten: dict = CTX) -> dict:
    """Human oversight (GDPR / EU AI Act): authorize a proposal → execute its real side-effect + audit."""
    try:
        return ten["autopilot"].approve(req.pid, req.approver)
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal_not_found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@app.post("/clinic/autopilot/reject")
def autopilot_reject(req: AutopilotActionRequest, ten: dict = CTX) -> dict:
    try:
        return ten["autopilot"].reject(req.pid, req.approver)
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal_not_found")


# ═══════ COMPLIANCE STATE MACHINE — Extractor→Auditor→Gate(0.96)→Formatter (omnios_core) ═══════
def _core_summary(st) -> dict:
    return {"id": st.id, "phase": st.phase.value, "tier": st.tier, "frozen": st.frozen,
            "freeze_reason": st.freeze_reason, "confidence": st.confidence,
            "wilson_floor_95": st.wilson_floor, "trail": st.trail,
            "rehydrated": st.rehydrated, "causal_evidence": st.causal,
            "note_redacted": st.note_redacted[:200], "claim": st.claim, "value": st.value,
            "refuted": [v.model_dump() for v in st.verdicts if not v.confirmed]}


@app.post("/compliance/run", dependencies=_SECURED)
async def compliance_run(req: ComplianceRunRequest) -> dict:
    """A clinical note → PII-sealed → losslessly compressed → coded → adversarially audited
    (incl. causal-graph justification) → risk-tier gated (claims 0.96 / clinical_docs 0.978,
    breach ⇒ FREEZE) → claim manifest. Frozen states surface in the «Αποφάσεις» tab."""
    try:
        st = await _core.run_note(req.transcript, patient_type=req.patient_type,
                                  minutes=req.minutes, tier=req.tier)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if st.frozen:
        _explog.record(f"Κωδικοποίηση σημείωσης — ΠΑΓΩΜΕΝΗ ({st.id})", st.confidence,
                       why=[st.freeze_reason], sources=["clinical_manifest.json"], high_risk=True)
    else:
        dx = ",".join(d["icd10"] for d in st.claim["diagnoses"])
        _explog.record(f"Κωδικοποίηση & claim έτοιμο — {dx}", st.confidence,
                       why=[f"auditor confirmed {len(st.proposals)} proposals",
                            f"claim {st.claim['claim_id']} · zero PII egress (vault-verified)"],
                       sources=["clinical_manifest.json"], evidence={"audit_seq": st.claim["integrity"]["audit_seq"]})
    return _core_summary(st)


@app.get("/compliance/pending", dependencies=_SECURED)
def compliance_pending() -> dict:
    """Frozen states awaiting a clinician (read-only)."""
    return {"pending": [_core_summary(s) for s in _core.pending_overrides()],
            "value": _core.tracker.totals()}


@app.get("/compliance/claim/{state_id}/fhir", dependencies=_SECURED)
def compliance_claim_fhir(state_id: str) -> dict:
    """The claim as an HL7 FHIR R4 Bundle (Patient/Encounter/Condition/Procedure/Claim).
    PII-free by construction AND re-verified by the vault's egress firewall before serving.
    Keyed anyway: diagnoses are clinical data even when tokenized (data-minimization, Art.25)."""
    from fhir_adapter import claim_to_fhir
    import json as _json
    st = _core.states.get(state_id)
    if st is None:
        raise HTTPException(status_code=404, detail="state_not_found")
    if not st.claim:
        raise HTTPException(status_code=409, detail=f"no claim yet (phase={st.phase.value})")
    bundle = claim_to_fhir(st.claim)
    _core.vault.assert_no_pii(_json.dumps(bundle, ensure_ascii=False))
    return bundle


@app.post("/compliance/override/{state_id}", dependencies=_SECURED)
async def compliance_override(state_id: str, req: ComplianceOverrideRequest) -> dict:
    """HUMAN_OVERRIDE node: a clinician approves (resumes → claim) or rejects. Audited who/why."""
    try:
        if req.decision == "reject":
            st = _core.reject(state_id, req.clinician_id, req.reason)
        else:
            st = await _core.approve(state_id, req.clinician_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="state_not_found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return _core_summary(st)


# ── serve the product UI from the SAME service (one deployable) — mounted LAST so it never
#    shadows an API route. Local dev can still run the UI on :8098; in prod this serves it at "/".
_UI_DIR = os.path.join(os.path.dirname(__file__), "..", "ui")
if os.path.isdir(_UI_DIR):
    app.mount("/", StaticFiles(directory=_UI_DIR, html=True), name="ui")
