"""
autonomous_loop.py (Ran-Tevu) — ο αυτόνομος βρόχος που ΜΑΘΑΙΝΕΙ, per-slug, σε layers.
================================================================================
Κάθε κύκλος (τρέχει server-side από τον scheduler, χωρίς prompt): παρατηρεί τα ΝΕΑ outcomes ενός
ιατρείου, ενημερώνει το no-show μοντέλο (κινείται προς τον αληθινό ρυθμό), υπολογίζει το ρίσκο των
επερχόμενων, και ΕΚΤΕΛΕΙ ανάκτηση για τα νέα no-shows (idempotent).

  L1 INGEST  — νέα no_show/completed bookings από το watermark.
  L2 LEARN   — Beta-Bernoulli update· persist (επιβιώνει restart, πίνακας autoloop_state).
  L3 DECIDE  — risk_brain.scan_upcoming: ποια επερχόμενα είναι υψηλού ρίσκου.
  L4 EXECUTE — για κάθε ΝΕΟ no-show ΜΕ καταγεγραμμένη συναίνεση επικοινωνίας ('reminders') και
               χωρίς ήδη recovery job → enqueue_outbound_job. Κάθε ενέργεια audited ξεχωριστά.
  L5 AUDIT   — aggregate log_audit (τι έμαθε + τι εκτέλεσε).

⚠️ CONSENT (Ν.3471/2006 / GDPR): το L4 στέλνει ΜΟΝΟ σε ασθενείς με ρητή συναίνεση επικοινωνίας
(get_consent_status(phone,'reminders').accepted is True) — ίδιο gate με τον CheckoutAgent. Χωρίς
καταγεγραμμένη συναίνεση ΔΕΝ γίνεται αυτόνομη επικοινωνία (ο autonomous βρόχος δεν κάνει auto-blast).
Ο έλεγχος γίνεται ΕΔΩ, στον producer· ΔΕΝ βασιζόμαστε σε downstream gate (το process_due_jobs δεν
ελέγχει consent). Αν ο γιατρός θέλει το recovery να πυροδοτείται για όλους, πρέπει να συλλέγει
συναίνεση στη ροή κράτησης (νομική απόφαση, όχι default).

Tests: python tests/test_autonomous_loop.py
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import database as db

_PRIOR, _STRENGTH = 0.23, 10.0


def _ensure_table():
    with db._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS autoloop_state (
                        slug TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
                        PRIMARY KEY (slug, key))""")
        c.commit()


def _get(slug, key, default=None):
    with db._conn() as c:
        r = c.execute("SELECT value FROM autoloop_state WHERE slug=? AND key=?", (slug, key)).fetchone()
    return json.loads(r["value"]) if r else default


def _put(slug, key, value):
    with db._conn() as c:
        c.execute("INSERT INTO autoloop_state (slug,key,value) VALUES (?,?,?) "
                  "ON CONFLICT(slug,key) DO UPDATE SET value=excluded.value",
                  (slug, key, json.dumps(value)))
        c.commit()


class Learner:
    """Beta-Bernoulli no-show estimate — κινείται από τον prior προς τον αληθινό ρυθμό."""
    def __init__(self, alpha=None, beta=None, n=0):
        self.alpha = alpha if alpha is not None else _PRIOR * _STRENGTH
        self.beta = beta if beta is not None else (1 - _PRIOR) * _STRENGTH
        self.n = n

    def observe(self, no_show: bool):
        if no_show:
            self.alpha += 1.0
        else:
            self.beta += 1.0
        self.n += 1

    def rate(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def to_dict(self):
        return {"alpha": self.alpha, "beta": self.beta, "n": self.n}


def _load_learner(slug) -> Learner:
    d = _get(slug, "learner")
    return Learner(**d) if d else Learner()


def _has_recovery(slug, booking_id) -> bool:
    with db._conn() as c:
        r = c.execute("SELECT 1 FROM outbound_jobs WHERE slug=? AND booking_id=? "
                      "AND job_type='no_show_recovery' LIMIT 1", (slug, booking_id)).fetchone()
    return bool(r)


def cycle(slug: str) -> dict:
    """Ένας πλήρης κύκλος των 5 layers για ένα ιατρείο."""
    _ensure_table()
    observed = set(_get(slug, "observed", []))
    learner = _load_learner(slug)
    rate_before = learner.rate()

    # ── L1 INGEST + L2 LEARN ────────────────────────────────────────────────
    new_obs = 0
    new_noshows = []
    for b in db.get_all_bookings(slug, limit=500):
        if b["status"] in ("completed", "no_show") and b["id"] not in observed:
            learner.observe(b["status"] == "no_show")
            observed.add(b["id"])
            new_obs += 1
            if b["status"] == "no_show":
                new_noshows.append(b)
    if new_obs:
        _put(slug, "learner", learner.to_dict())
        _put(slug, "observed", sorted(observed))
    rate_after = learner.rate()

    # ── L3 DECIDE: ρίσκο επερχόμενων ────────────────────────────────────────
    high_risk = 0
    try:
        from risk_brain import scan_upcoming
        scan = scan_upcoming(slug, 7)
        high_risk = len(scan.get("high", [])) if isinstance(scan, dict) else 0
    except Exception:
        pass

    # ── L4 EXECUTE: ανάκτηση για νέα no-shows — CONSENT-GATED (opt-in), idempotent ──
    enqueued = skipped_no_consent = 0
    for b in new_noshows:
        if not b.get("phone") or _has_recovery(slug, b["id"]):
            continue
        # CONSENT GATE (Ν.3471/2006): ο producer ΔΕΝ βασίζεται σε downstream gate — ελέγχει ΕΔΩ.
        # Στέλνει ΜΟΝΟ αν υπάρχει ρητή καταγεγραμμένη συναίνεση 'reminders' (ίδιο με CheckoutAgent).
        cons = db.get_consent_status(b["phone"], consent_type="reminders")
        if not (cons and cons.get("accepted")):
            skipped_no_consent += 1
            continue
        try:
            from database import enqueue_outbound_job
            nxt = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
            if nxt <= datetime.now():
                nxt += timedelta(days=1)
            enqueue_outbound_job(slug=slug, job_type="no_show_recovery",
                                 customer_phone=b["phone"], customer_name=b.get("name", ""),
                                 customer_email=b.get("email", ""), booking_id=b["id"],
                                 scheduled_at=nxt.strftime("%Y-%m-%d %H:%M:%S"), channel="sms",
                                 payload={"booking_date": b["date"], "booking_time": b["time"],
                                          "service": b.get("service", "")})
            enqueued += 1
            # per-action audit (tamper-evident, EU AI Act Art.12) — booking id ΜΟΝΟ, όχι raw PII
            try:
                db.log_audit(action="autoloop.recovery_enqueued", slug=slug, actor_type="agent",
                             actor_id="autonomous_loop", details=str(b["id"]))
            except Exception:
                pass
        except Exception as e:
            print(f"[autoloop] recovery enqueue failed ({slug} #{b['id']}): {e}")

    # ── L5 AUDIT ────────────────────────────────────────────────────────────
    try:
        db.log_audit(action="autoloop.cycle", slug=slug, actor_type="agent", actor_id="autonomous_loop",
                     details=f"observed={new_obs} rate={rate_after:.3f} recovery_enqueued={enqueued} "
                             f"skipped_no_consent={skipped_no_consent} high_risk={high_risk}")
    except Exception:
        pass
    return {"observed_new": new_obs, "learned_no_show_before": round(rate_before, 4),
            "learned_no_show_after": round(rate_after, 4), "total_observations": learner.n,
            "high_risk_upcoming": high_risk, "recovery_enqueued": enqueued,
            "skipped_no_consent": skipped_no_consent}


def state(slug: str) -> dict:
    _ensure_table()
    learner = _load_learner(slug)
    return {"learned_no_show_prob": round(learner.rate(), 4), "observations": learner.n, "prior": _PRIOR}


def run_all_clinics() -> dict:
    """Ένας κύκλος για ΚΑΘΕ ιατρείο — καλείται από τον scheduler (server-side, χωρίς prompt)."""
    _ensure_table()
    out = {"clinics": 0, "observed": 0, "recovery_enqueued": 0}
    try:
        with db._conn() as c:
            slugs = [r["slug"] for r in c.execute("SELECT slug FROM businesses").fetchall()]
    except Exception:
        slugs = []
    for slug in slugs:
        try:
            r = cycle(slug)
            out["clinics"] += 1
            out["observed"] += r["observed_new"]
            out["recovery_enqueued"] += r["recovery_enqueued"]
        except Exception as e:
            print(f"[autoloop] cycle failed for {slug}: {e}")
    return out
