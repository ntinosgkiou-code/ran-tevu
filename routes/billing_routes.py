"""
billing_routes.py — Recurring Stripe subscription billing (€179/μήνα flat + 14d trial).

Το #2 leverage move (LEVERAGE-ROADMAP impact 10): self-serve billing → το $1M
γίνεται εφικτό χωρίς sales team. Ο πελάτης κάνει checkout μόνος του, trial 14
μερών ΧΩΡΙΣ κάρτα (payment_method_collection='if_required'), μετά €179/μήνα auto.

Env που χρειάζεται (ΗΔΗ set στο Railway):
- STRIPE_SECRET_KEY      (sk_test / sk_live)
- STRIPE_WEBHOOK_SECRET  (whsec_... — από Stripe dashboard webhook)
- PUBLIC_BASE_URL        (π.χ. https://www.ran-tevu.gr)

ΑΡΧΗ: idempotent webhooks (UNIQUE σε stripe_subscription_id), best-effort, ποτέ
δεν σκάει το request αν λείπει config (graceful degrade σε local χωρίς keys).
"""
import os
from flask import Blueprint, request, jsonify, redirect

import database as db

bp = Blueprint("billing", __name__)

PRICE_EUR_CENTS = 17900          # €179.00 (Standard)
TRIAL_DAYS = 14
PLAN_NAME = "Ran-Tevu AI Receptionist"

# ── Δύο πακέτα ─────────────────────────────────────────────────────────────────
# Standard: AI ρεσεψιόν (booking + reminders + dashboard + back-office view).
# Premium:  + Revenue Recovery Autopilot + AI Γραμματέας (φυσική γλώσσα εκτέλεση).
PLANS = {
    "standard": {"cents": 17900, "name": "Ran-Tevu Standard — AI Ρεσεψιόν"},
    "premium":  {"cents": 29900, "name": "Ran-Tevu Premium — AI Ρεσεψιόν + Revenue Recovery"},
}
DEFAULT_TIER = "standard"


def _normalize_tier(t):
    t = (t or "").strip().lower()
    return t if t in PLANS else DEFAULT_TIER


# ── DB ───────────────────────────────────────────────────────────────────────
def ensure_table():
    """Creates subscriptions table (idempotent) + migrates tier column."""
    with db._conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                slug                   TEXT,
                customer_email         TEXT,
                business_name          TEXT,
                stripe_customer_id     TEXT,
                stripe_subscription_id TEXT UNIQUE,
                status                 TEXT,
                plan                   TEXT DEFAULT 'flat_149',
                tier                   TEXT DEFAULT 'standard',
                trial_ends_at          TEXT,
                current_period_end     TEXT,
                created                TEXT DEFAULT (datetime('now')),
                updated                TEXT DEFAULT (datetime('now'))
            )
        """)
        # Migration για υπάρχουσες βάσεις (CREATE IF NOT EXISTS δεν προσθέτει στήλη)
        try:
            c.execute("ALTER TABLE subscriptions ADD COLUMN tier TEXT DEFAULT 'standard'")
        except Exception:
            pass  # στήλη υπάρχει ήδη


def plan_tier(slug):
    """Tier της συνδρομής ενός slug: 'premium' | 'standard' | None (χωρίς sub = demo/internal)."""
    if not slug:
        return None
    ensure_table()
    with db._conn() as c:
        row = c.execute(
            "SELECT tier FROM subscriptions WHERE slug=? ORDER BY updated DESC LIMIT 1", (slug,)
        ).fetchone()
    if not row:
        return None
    return (row["tier"] or "standard").strip().lower()


def is_premium(slug):
    """True αν premium tier Ή χωρίς συνδρομή (demo/internal = πλήρης πρόσβαση).
    Μόνο ρητά 'standard' πελάτες ΔΕΝ έχουν τα premium features."""
    return plan_tier(slug) != "standard"


def _upsert_subscription(sub_id, **fields):
    """Idempotent upsert keyed on stripe_subscription_id."""
    if not sub_id:
        return
    ensure_table()
    cols = ["stripe_subscription_id"] + list(fields.keys())
    with db._conn() as c:
        row = c.execute(
            "SELECT id FROM subscriptions WHERE stripe_subscription_id=?", (sub_id,)
        ).fetchone()
        if row:
            # ΜΟΝΟ μη-None πεδία στο UPDATE: partial events (π.χ. subscription.updated που
            # δίνει slug=None) δεν πρέπει να σβήνουν το slug/tier που έγραψε το checkout.completed.
            upd = {k: v for k, v in fields.items() if v is not None}
            if not upd:
                return
            sets = ", ".join(f"{k}=?" for k in upd) + ", updated=datetime('now')"
            c.execute(f"UPDATE subscriptions SET {sets} WHERE stripe_subscription_id=?",
                      tuple(upd.values()) + (sub_id,))
        else:
            placeholders = ", ".join("?" for _ in cols)
            c.execute(f"INSERT INTO subscriptions ({', '.join(cols)}) VALUES ({placeholders})",
                      (sub_id,) + tuple(fields.values()))


def subscription_status_for(slug):
    """Returns the live subscription status for a business slug, or None."""
    ensure_table()
    with db._conn() as c:
        row = c.execute(
            "SELECT status, trial_ends_at, current_period_end FROM subscriptions "
            "WHERE slug=? ORDER BY updated DESC LIMIT 1", (slug,)
        ).fetchone()
    return dict(row) if row else None


def subscription_blocked(slug):
    """True ΜΟΝΟ αν η συνδρομή έχει όντως ακυρωθεί/λήξει. Trial, σε εξέλιξη, ή abandoned
    checkout → False (ο γιατρός μπαίνει κανονικά στο back-office στη δοκιμή).
    Businesses ΧΩΡΙΣ subscription (demo/internal) → False."""
    s = subscription_status_for(slug)
    if not s:
        return False
    status = (s.get("status") or "").lower()
    # trialing/active/incomplete/κενό → ΠΟΤΕ block (ο γιατρός δοκιμάζει)· incomplete_expired = abandoned checkout, ΟΧΙ block
    if status in ("trialing", "active", "incomplete", "incomplete_expired", "paused", ""):
        return False
    return status in ("canceled", "cancelled", "past_due", "unpaid")


def paywall_response():
    """Σελίδα 'ανανέωσε συνδρομή' (402) για owner routes όταν η συνδρομή έληξε."""
    html = ("<!DOCTYPE html><html lang='el'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Συνδρομή</title></head>"
            "<body style='font-family:-apple-system,Segoe UI,sans-serif;background:#f6f8fb;"
            "color:#0f172a;text-align:center;padding:60px 20px'>"
            "<h2 style='color:#2563eb'>Η συνδρομή σου έληξε</h2>"
            "<p style='color:#475569'>Ανανέωσε για να ξαναποκτήσεις πρόσβαση στο dashboard &amp; back-office.</p>"
            "<a href='/pricing' style='display:inline-block;margin-top:16px;background:#2563eb;color:#fff;"
            "padding:12px 26px;border-radius:10px;text-decoration:none;font-weight:700'>Ανανέωση συνδρομής</a>"
            "</body></html>")
    return html, 402, {"Content-Type": "text/html; charset=utf-8"}


def _stripe():
    """Lazy stripe client; returns None if not configured."""
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        return None
    import stripe
    stripe.api_key = key
    return stripe


# ── Checkout ──────────────────────────────────────────────────────────────────
@bp.post("/api/billing/checkout")
def create_checkout():
    """Creates a Stripe Checkout Session (subscription, 14d trial, no card upfront)."""
    stripe = _stripe()
    if stripe is None:
        return jsonify({"error": "billing_not_configured"}), 503

    data = request.get_json(silent=True) or request.form
    email = (data.get("email") or "").strip() or None
    biz_name = (data.get("business_name") or "").strip() or None
    vertical = (data.get("vertical") or data.get("specialty") or "").strip() or None
    tier = _normalize_tier(data.get("tier") or data.get("plan"))
    plan = PLANS[tier]

    base = os.environ.get("PUBLIC_BASE_URL", "https://www.ran-tevu.gr").rstrip("/")
    # metadata → φτάνει στο webhook → auto-provision. Το slug δημιουργείται εκεί.
    meta = {"business_name": biz_name or "", "vertical": vertical or "",
            "email": email or "", "tier": tier}
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_collection="if_required",   # no card needed for trial
            customer_email=email,
            line_items=[{
                "quantity": 1,
                "price_data": {
                    "currency": "eur",
                    "unit_amount": plan["cents"],
                    "recurring": {"interval": "month"},
                    "product_data": {"name": plan["name"]},
                },
            }],
            subscription_data={
                "trial_period_days": TRIAL_DAYS,
                "metadata": meta,
            },
            metadata=meta,
            success_url=f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/billing/cancel",
            allow_promotion_codes=True,
        )
        # Α) ΑΜΕΣΟ email μόλις βάλει email (πριν καν ολοκληρώσει το Stripe) — να μη χαθεί κανείς
        if email:
            try:
                from email_service import send_email_via_resend
                send_email_via_resend(
                    email, "Ξεκίνα το Ran-Tevu — ένα βήμα ακόμα",
                    (f"<p>Γεια σας! Λάβαμε την εγγραφή σας"
                     f"{(' για ' + biz_name) if biz_name else ''}.</p>"
                     f"<p>Ολοκληρώστε το <b>δωρεάν 14ήμερο</b> (χωρίς κάρτα) εδώ:</p>"
                     f"<p><a href='{session.url}'>➡️ Ολοκλήρωση εγγραφής</a></p>"
                     f"<p>Μόλις το ολοκληρώσετε, το ιατρείο σας στήνεται <b>αυτόματα</b> και "
                     f"λαμβάνετε τα στοιχεία σας.</p><p>— Κωνσταντίνος, Ran-Tevu</p>"),
                    from_name="Κωνσταντίνος (Ran-Tevu)")
            except Exception as _e:
                print(f"[BILLING] instant signup email skipped: {_e}")
        return jsonify({"url": session.url, "id": session.id})
    except Exception as ex:
        print(f"[BILLING] checkout failed: {ex}")
        return jsonify({"error": "checkout_failed", "detail": str(ex)}), 500


# ── Webhook ───────────────────────────────────────────────────────────────────
@bp.post("/webhooks/stripe")
def stripe_webhook():
    """Handles Stripe subscription lifecycle events. Idempotent."""
    stripe = _stripe()
    if stripe is None:
        return jsonify({"error": "billing_not_configured"}), 503

    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

    try:
        if secret:
            event = stripe.Webhook.construct_event(payload, sig, secret)
        else:
            # Local/dev without secret: parse unverified (never on production path)
            import json
            event = json.loads(payload)
            print("[BILLING] WARNING: webhook signature NOT verified (no secret set)")
    except Exception as ex:
        print(f"[BILLING] webhook signature failed: {ex}")
        return jsonify({"error": "invalid_signature"}), 400

    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    try:
        if etype == "checkout.session.completed":
            sub_id = obj.get("subscription")
            meta = obj.get("metadata", {}) or {}
            email = ((obj.get("customer_details", {}) or {}).get("email")
                     or obj.get("customer_email") or meta.get("email") or None)
            # Idempotency: αν έχει ήδη γίνει provision (υπάρχει slug), μην ξαναστήσεις
            slug = None
            try:
                ensure_table()
                with db._conn() as c:
                    r = c.execute(
                        "SELECT slug FROM subscriptions WHERE stripe_subscription_id=? "
                        "AND slug IS NOT NULL AND slug != ''", (sub_id,)).fetchone()
                    slug = r["slug"] if r else None
            except Exception:
                slug = None
            if not slug:
                # ΑΥΤΟΜΑΤΟ ΣΤΗΣΙΜΟ: πελάτης πλήρωσε → live ιατρείο + welcome email (όχι demo data)
                try:
                    import provisioning
                    res = provisioning.provision(
                        name=(meta.get("business_name") or "Το Ιατρείο μου"),
                        vertical_key=(meta.get("vertical") or "medical"),
                        email=email or "", demo=False)
                    slug = res.get("slug")
                    _send_welcome_email(email, meta.get("business_name"), res.get("links", {}))
                    print(f"[BILLING] auto-provisioned slug={slug} for sub={sub_id}")
                except Exception as ex:
                    print(f"[BILLING] auto-provision FAILED for sub={sub_id}: {ex}")
            _upsert_subscription(
                sub_id,
                slug=slug,
                business_name=meta.get("business_name") or None,
                customer_email=email,
                stripe_customer_id=obj.get("customer"),
                status="trialing",
                tier=_normalize_tier(meta.get("tier")),
            )
            print(f"[BILLING] checkout completed → sub={sub_id} slug={slug} tier={meta.get('tier')}")

        elif etype in ("customer.subscription.created", "customer.subscription.updated"):
            meta = obj.get("metadata", {}) or {}
            # Stripe API 2025-03-31.basil+ removed current_period_end from the top-level
            # Subscription object — it moved onto each subscription item. Fall back to the
            # top-level key for older API versions/payloads so this reads correctly either way.
            period_end = obj.get("current_period_end")
            if period_end is None:
                items = ((obj.get("items") or {}).get("data")) or []
                if items:
                    period_end = items[0].get("current_period_end")
            _upsert_subscription(
                obj.get("id"),
                slug=meta.get("slug") or None,
                stripe_customer_id=obj.get("customer"),
                status=obj.get("status"),
                trial_ends_at=_ts(obj.get("trial_end")),
                current_period_end=_ts(period_end),
            )
            print(f"[BILLING] subscription {obj.get('status')} → {obj.get('id')}")

        elif etype == "customer.subscription.deleted":
            _upsert_subscription(obj.get("id"), status="canceled")
            print(f"[BILLING] subscription canceled → {obj.get('id')}")

        elif etype == "invoice.payment_failed":
            sub_id = obj.get("subscription")
            _upsert_subscription(sub_id, status="past_due")
            print(f"[BILLING] payment failed → sub={sub_id}")

    except Exception as ex:
        print(f"[BILLING] webhook handler error ({etype}): {ex}")
        # Return 200 anyway so Stripe doesn't retry-storm on our bug
        return jsonify({"received": True, "warning": str(ex)}), 200

    return jsonify({"received": True})


def _ts(unix):
    """Stripe unix seconds → ISO date string (or None)."""
    if not unix:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(unix), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _send_welcome_email(email, business_name, links, owner_name=""):
    """Πλήρες welcome + ΟΔΗΓΟΣ: πού μπαίνει ο γιατρός, τι κάνει πρώτα, πώς δουλεύει."""
    if not email:
        return
    try:
        from email_service import send_email_via_resend
        name = business_name or "το ιατρείο σου"
        hi = f"Καλωσόρισες{(' ' + owner_name) if owner_name else ''}!"
        setup = links.get("setup", ""); chat = links.get("chat", "")
        dash = links.get("dashboard", ""); boff = links.get("back_office", "")
        btn = ("display:inline-block;background:#2563eb;color:#fff;text-decoration:none;"
               "padding:13px 22px;border-radius:10px;font-weight:700;font-size:15px")
        card = "background:#f8fafc;border:1px solid #e8edf5;border-radius:12px;padding:16px;margin:12px 0"
        html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:580px;margin:auto;color:#0f172a;font-size:15px;line-height:1.6">
<h2 style="color:#2563eb;margin-bottom:4px">{hi}</h2>
<p>Το <b>{name}</b> είναι <b>live</b> 🎉 — ξεκίνησες το 14ήμερο δωρεάν, δεν χρεώθηκες τίποτα.</p>

<div style="{card};background:#eff6ff;border-color:#bfdbfe;text-align:center">
  <p style="margin:0 0 12px"><b>👉 Μπες στο ιατρείο σου από εδώ</b><br/>
  <span style="color:#64748b;font-size:13px">Αυτός είναι ο πίνακάς σου — ραντεβού, ασθενείς, έσοδα, όλα σε ένα.</span></p>
  <a href="{boff}" style="{btn}">🔓 Άνοιξε το ιατρείο μου</a>
</div>

<h3 style="margin:22px 0 8px">Τι να κάνεις πρώτα (5 λεπτά):</h3>
<div style="{card}"><b>1. Στήσε ώρες & υπηρεσίες/τιμές</b> — ώστε η AI να κλείνει σωστά ραντεβού.<br/>
  <a href="{setup}">→ Στήσιμο ιατρείου</a></div>
<div style="{card}"><b>2. Βάλε το link κρατήσεων</b> στο site/Instagram/Google σου — οι ασθενείς κλείνουν μόνοι τους, 24/7.<br/>
  <a href="{chat}">→ Η σελίδα κρατήσεών σου</a></div>
<div style="{card}"><b>3. Έχεις παλιούς ασθενείς;</b> Ανέβασέ τους από Excel μέσα στο ιατρείο (κουμπί «📥 Εισαγωγή») — έτσι ξεκινά το win-back να φέρνει πίσω όσους χάθηκαν.</div>
<div style="{card}"><b>4. Τηλεφωνική AI (προαιρετικό):</b> από το Στήσιμο πάτα «🎙️ Στήσε τη φωνή» και κάνε προώθηση το τηλέφωνό σου εκεί.</div>

<h3 style="margin:22px 0 8px">Πώς δουλεύει (χωρίς να κάνεις τίποτα):</h3>
<p>Η AI γραμματέας σηκώνει τηλέφωνα & μηνύματα 24/7, κλείνει ραντεβού, στέλνει υπενθυμίσεις,
και <b>φέρνει πίσω αυτόματα</b> ασθενείς που δεν ήρθαν ή χάθηκαν. Εσύ απλά ανοίγεις το ιατρείο σου
και βλέπεις <b>πόσα € σου γύρισε πίσω</b> — με ονόματα.</p>

<p style="margin-top:18px">📊 <a href="{dash}">Έσοδα & ανακτήσεις</a> · ❓ Ερωτήσεις; <b>Απάντησε απλά σε αυτό το email.</b></p>
<p style="color:#64748b;font-size:13px;margin-top:18px">— Κωνσταντίνος, Ran-Tevu</p></div>"""
        send_email_via_resend(email, f"Το ιατρείο σου είναι έτοιμο — ξεκίνα εδώ ({name})", html)
    except Exception as ex:
        print(f"[BILLING] welcome email failed: {ex}")


def _ensure_provisioned_from_session(session_id: str):
    """FALLBACK provisioning στο success page: αν το async Stripe webhook δεν πρόλαβε ή απέτυχε,
    στήνουμε το ιατρείο ΕΔΩ ώστε ο πληρωμένος γιατρός να ΜΗΝ μείνει ποτέ κλειδωμένος έξω.
    Idempotent — αν έχει ήδη γίνει provision, επιστρέφει το υπάρχον slug χωρίς διπλό στήσιμο."""
    stripe = _stripe()
    if stripe is None:
        return None
    try:
        sess = stripe.checkout.Session.retrieve(session_id)
    except Exception as ex:
        print(f"[BILLING] success: session retrieve failed: {ex}")
        return None
    sub_id = sess.get("subscription")
    meta = sess.get("metadata", {}) or {}
    email = ((sess.get("customer_details", {}) or {}).get("email")
             or sess.get("customer_email") or meta.get("email") or None)
    ensure_table()
    try:
        with db._conn() as c:
            r = c.execute("SELECT slug FROM subscriptions WHERE stripe_subscription_id=? "
                          "AND slug IS NOT NULL AND slug != ''", (sub_id,)).fetchone()
            if r and r["slug"]:
                return r["slug"]      # ήδη provisioned (από το webhook) — μην ξαναστήσεις
    except Exception:
        pass
    try:
        import provisioning
        res = provisioning.provision(name=(meta.get("business_name") or "Το Ιατρείο μου"),
                                     vertical_key=(meta.get("vertical") or "medical"),
                                     email=email or "", demo=False)
        slug = res.get("slug")
        _send_welcome_email(email, meta.get("business_name"), res.get("links", {}))
        _upsert_subscription(sub_id, slug=slug, business_name=meta.get("business_name") or None,
                             customer_email=email, stripe_customer_id=sess.get("customer"),
                             status="trialing", tier=_normalize_tier(meta.get("tier")))
        print(f"[BILLING] FALLBACK-provisioned slug={slug} for sub={sub_id} (success page)")
        return slug
    except Exception as ex:
        print(f"[BILLING] success fallback provision FAILED: {ex}")
        return None


# ── Success / Cancel pages ────────────────────────────────────────────────────
@bp.get("/billing/success")
def billing_success():
    # Fallback: εγγυημένο στήσιμο + άμεσο link, να μην εξαρτάται από το async webhook.
    boff_btn = ""
    try:
        sid = request.args.get("session_id", "")
        if sid:
            slug = _ensure_provisioned_from_session(sid)
            if slug:
                import provisioning
                boff = provisioning._links(slug).get("back_office", "")
                if boff:
                    boff_btn = f'<a href="{boff}">🔓 Άνοιξε το ιατρείο σου τώρα</a>'
    except Exception as ex:
        print(f"[BILLING] success page error: {ex}")
    _cta = boff_btn or '<a href="/">Πίσω στην αρχική →</a>'
    return ("""<!DOCTYPE html><html lang="el"><head><meta charset="utf-8">
<title>Ξεκινήσαμε! — Ran-Tevu</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:-apple-system,sans-serif;background:#050510;color:#E5E7EB;display:flex;
min-height:100vh;align-items:center;justify-content:center;text-align:center;margin:0}
.box{max-width:480px;padding:40px}.icon{font-size:64px}h1{color:#fff;font-size:28px;margin:16px 0}
p{color:rgba(255,255,255,0.7);line-height:1.6}a{display:inline-block;margin-top:24px;background:
linear-gradient(135deg,#FBBF24,#F59E0B);color:#050510;padding:14px 28px;border-radius:12px;
font-weight:800;text-decoration:none}</style></head><body><div class="box">
<div class="icon">🎉</div><h1>Ξεκίνησε το 14ήμερο δωρεάν!</h1>
<p>Δεν χρεώθηκες τίποτα. Θα λάβεις email με τα επόμενα βήματα για να στήσουμε το AI receptionist σου.
Μέσα στις 14 μέρες βλέπεις πόσα ραντεβού σου σώζει — και μόνο τότε αποφασίζεις.</p>
__CTA__</div></body></html>""".replace("__CTA__", _cta),
            200, {"Content-Type": "text/html; charset=utf-8"})


@bp.get("/billing/cancel")
def billing_cancel():
    return redirect("/pricing")


@bp.post("/billing/resend-link")
def billing_resend_link():
    """Self-serve recovery: ο γιατρός έχασε το link του → βάζει το email του και του το ξαναστέλνουμε.
    Anti-enumeration: πάντα ίδια γενική απάντηση, δεν αποκαλύπτει αν υπάρχει το email."""
    data = request.get_json(silent=True) or {}
    email = (request.form.get("email") or data.get("email") or "").strip().lower()
    generic = jsonify({"ok": True,
                       "msg": "Αν υπάρχει ιατρείο με αυτό το email, θα λάβεις το link σε λίγο."})
    if not email:
        return generic
    try:
        ensure_table()
        with db._conn() as c:
            r = c.execute("SELECT slug, business_name FROM subscriptions "
                          "WHERE lower(customer_email)=? AND slug IS NOT NULL AND slug != '' "
                          "ORDER BY updated DESC LIMIT 1", (email,)).fetchone()
        if r and r["slug"]:
            import provisioning
            _send_welcome_email(email, r["business_name"], provisioning._links(r["slug"]))
            print(f"[BILLING] resent link for slug={r['slug']}")
    except Exception as ex:
        print(f"[BILLING] resend-link error: {ex}")
    return generic


# ── Self-serve subscription management (Stripe Customer Portal) ────────────────
@bp.post("/api/<slug>/billing/portal")
def billing_portal(slug):
    """Ανοίγει το Stripe Customer Portal για να διαχειριστεί ο γιατρός τη συνδρομή του
    (ακύρωση/αλλαγή κάρτας/τιμολόγια) μόνος του. Customer-token-gated (dash_token ή owner)."""
    # Auth: ίδιο μοντέλο με dashboard/back-office (HMAC dash_token ή owner cookie)
    authed = False
    try:
        from routes.dashboard_routes import dash_token
        import hmac as _hmac
        authed = _hmac.compare_digest(request.args.get("t", ""), dash_token(slug))
    except Exception:
        authed = False
    if not authed:
        try:
            from routes.start_routes import is_owner
            authed = is_owner()
        except Exception:
            authed = False
    if not authed:
        return jsonify({"error": "unauthorized"}), 403

    stripe = _stripe()
    if stripe is None:
        return jsonify({"error": "billing_not_configured"}), 503
    ensure_table()
    with db._conn() as c:
        row = c.execute(
            "SELECT stripe_customer_id FROM subscriptions WHERE slug=? "
            "AND stripe_customer_id IS NOT NULL AND stripe_customer_id != '' "
            "ORDER BY updated DESC LIMIT 1", (slug,)).fetchone()
    if not row:
        return jsonify({"error": "no_subscription",
                        "msg": "Δεν βρέθηκε ενεργή συνδρομή για διαχείριση."}), 404
    base = os.environ.get("PUBLIC_BASE_URL", "https://www.ran-tevu.gr").rstrip("/")
    try:
        sess = stripe.billing_portal.Session.create(
            customer=row["stripe_customer_id"],
            return_url=f"{base}/{slug}/back-office")
        return jsonify({"url": sess.url})
    except Exception as ex:
        print(f"[BILLING] portal failed: {ex}")
        return jsonify({"error": "portal_failed", "detail": str(ex)}), 500


def register(app):
    """Mirror of other route modules' register() — ensures table + registers bp."""
    ensure_table()
    app.register_blueprint(bp)
