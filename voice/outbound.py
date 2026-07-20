"""
voice/outbound.py — Outbound Proactive AI (Feature #3, 2026-05-28)

Wave-ahead: AI calls customers PROACTIVELY (not just reactively).
Job types:
  - pre_arrival_upsell  : 3 days before booking → offer transfer/upgrade/spa
  - no_show_reminder    : 4 hours before booking → confirm attendance
  - review_request      : 1 day after booking   → ask for Google review
  - win_back            : 90+ days inactive customer → re-engage with discount

Architecture:
  1. enqueue_*_for_booking()   — auto-triggered when booking is created
  2. process_due_jobs()        — APScheduler runs every 5 min
  3. _execute_voice_job()      — uses voice.vapi_client.create_outbound_call()
  4. _execute_sms_job()        — uses voice.twilio_client.send_sms()
  5. _execute_email_job()      — uses email_service

Cost guard: each job has retry_max=2. Failures are logged + marked failed.
Telephony: Vapi outbound bound to business's existing assistant + phone number.
"""
from __future__ import annotations

import os
import json
import threading
from datetime import datetime, timedelta
from typing import Optional

import database as db
from voice import vapi_client, twilio_client


# ── Job script generators ─────────────────────────────────────────────

def _script_pre_arrival_upsell(business_name: str, customer_name: str,
                                date_str: str, time_str: str, service: str) -> str:
    """Greek pre-arrival upsell script for Vapi."""
    return (
        f"Καλείτε εκ μέρους του {business_name} τον/την {customer_name or 'πελάτη'}. "
        f"Έχει κράτηση στις {date_str} {time_str} για {service}. "
        f"ΣΤΟΧΟΣ: Με ζεστό tone, ρωτάς αν θέλει 3 πιθανά upsells:\n"
        f"  1) Airport transfer (εάν είναι ξενοδοχείο) ή early arrival\n"
        f"  2) Πρόσθετο service (massage, spa, extra appointment)\n"
        f"  3) Special request (dietary, parking, etc.)\n\n"
        f"ΟΧΙ pushy. Μάλλον σαν 'concierge call' — προστιθέμενη αξία.\n"
        f"Εάν δεν θέλει τίποτα, ευχαριστείς, λες 'τα λέμε στις {date_str}'.\n"
        f"Διάρκεια: 60-90 δευτερόλεπτα."
    )


def _script_no_show_reminder(business_name: str, customer_name: str,
                              date_str: str, time_str: str, service: str) -> str:
    return (
        f"Καλείτε εκ μέρους του {business_name} τον/την {customer_name or 'πελάτη'}. "
        f"Σε 4 ώρες έχει ραντεβού στις {time_str} για {service}.\n"
        f"ΣΤΟΧΟΣ: Επιβεβαίωση παρουσίας — απλό 'θα έρθετε;'.\n"
        f"Εάν 'ναι' → ευχαρίστηση + 'τα λέμε σε λίγο'.\n"
        f"Εάν 'όχι/ακύρωση' → προσφορά rebook σε άλλη ημερομηνία.\n"
        f"Εάν 'καθυστερώ' → λες 'OK θα ενημερώσω, μη βιάζεστε'.\n"
        f"Διάρκεια: 30-45 δευτερόλεπτα. Σύντομο."
    )


def _script_review_request(business_name: str, customer_name: str,
                            review_url: str = "") -> str:
    cta = f" Θα σας στείλουμε SMS με link." if not review_url else f" Είστε εδώ: {review_url}"
    return (
        f"Καλείτε εκ μέρους του {business_name} τον/την {customer_name or 'πελάτη'}. "
        f"Χθες ήρθε για ραντεβού.\n"
        f"ΣΤΟΧΟΣ: Σύντομη courtesy call — 'πώς πήγε η εμπειρία;'.\n"
        f"Εάν θετική → ζητάς review στο Google.{cta}\n"
        f"Εάν αρνητική → ζητάς feedback, λες 'θα ενημερώσω τον υπεύθυνο' (escalate σε owner).\n"
        f"Διάρκεια: 30 δευτερόλεπτα. Πολύ σύντομο."
    )


def _script_win_back(business_name: str, customer_name: str, last_visit: str = "") -> str:
    return (
        f"Καλείτε εκ μέρους του {business_name} τον/την {customer_name or 'παλιό πελάτη'}. "
        f"Δεν έχει έρθει εδώ και 90+ μέρες.\n"
        f"ΣΤΟΧΟΣ: Ζεστή re-engagement call.\n"
        f"Λες: 'Σας θυμηθήκαμε. Έχουμε 15% έκπτωση για επανερχόμενους.'\n"
        f"Aν ενδιαφέρεται → προτείνεις 2-3 διαθέσιμες ώρες, κλείνεις.\n"
        f"Aν όχι → ευγενικό 'τα λέμε όποτε θέλετε, καμία πίεση'.\n"
        f"Διάρκεια: 45-60 δευτερόλεπτα."
    )


SCRIPT_BUILDERS = {
    "pre_arrival_upsell": _script_pre_arrival_upsell,
    "no_show_reminder":   _script_no_show_reminder,
    "review_request":     _script_review_request,
    "win_back":           _script_win_back,
}


# ── Enqueue helpers (called from booking creation/cancellation) ───────

def enqueue_jobs_for_new_booking(slug: str, booking_id: int, booking: dict,
                                 lead_minutes: dict = None) -> dict:
    """Auto-create the standard outbound sequence when a booking is created.

    Default schedule:
      - pre_arrival_upsell: 3 days BEFORE  (only if booking is >= 4 days away)
      - no_show_reminder:   4 hours BEFORE
      - review_request:     24 hours AFTER
    """
    lead_minutes = lead_minutes or {
        "pre_arrival_upsell": -3 * 24 * 60,  # 3 days before
        "no_show_reminder":   -4 * 60,        # 4 hours before
        "review_request":      24 * 60,       # 1 day after
    }

    if not booking.get("phone"):
        return {"enqueued": 0, "skipped_reason": "no_phone"}

    try:
        booking_dt = datetime.strptime(
            f"{booking['date']} {booking['time']}", "%Y-%m-%d %H:%M"
        )
    except (KeyError, ValueError) as e:
        return {"enqueued": 0, "skipped_reason": f"bad_datetime: {e}"}

    now = datetime.now()
    enqueued = 0
    for job_type, offset_min in lead_minutes.items():
        scheduled = booking_dt + timedelta(minutes=offset_min)
        # Skip if scheduled time already passed (or < 30 min from now)
        if scheduled <= now + timedelta(minutes=30):
            continue
        payload = {
            "booking_date": booking["date"],
            "booking_time": booking["time"],
            "service": booking.get("service", "ραντεβού"),
        }
        jid = db.enqueue_outbound_job(
            slug=slug,
            job_type=job_type,
            customer_phone=booking["phone"],
            customer_name=booking.get("name", ""),
            customer_email=booking.get("email", ""),
            booking_id=booking_id,
            scheduled_at=scheduled.strftime("%Y-%m-%d %H:%M:%S"),
            channel="voice",
            payload=payload,
            dedup=True,
        )
        if jid:
            enqueued += 1
    return {"enqueued": enqueued}


def cancel_jobs_for_cancelled_booking(booking_id: int) -> int:
    """When booking is cancelled, cancel all related outbound jobs."""
    return db.cancel_outbound_jobs_for_booking(booking_id)


# ── P5 Safety rails ───────────────────────────────────────────────────

DAILY_CAP_PER_BUSINESS = int(os.environ.get("OUTBOUND_DAILY_CAP", "30"))
QUIET_START = 9    # καμία κλήση/SMS πριν τις 09:00 Athens
QUIET_END_H, QUIET_END_M = 20, 30  # ...ούτε μετά τις 20:30

# Permanent failures: retry δεν θα βοηθήσει — fail αμέσως.
_PERMANENT_FAIL_PREFIXES = (
    "business_not_found", "no_voice_agent_provisioned", "voice_agent_missing_ids",
    "no_script_for_", "unknown_job_type", "unknown_channel", "no_email",
)


def _within_contact_hours(now: datetime = None) -> bool:
    """True αν επιτρέπεται επικοινωνία με πελάτες (09:00-20:30 Athens)."""
    try:
        from zoneinfo import ZoneInfo
        now = now or datetime.now(ZoneInfo("Europe/Athens"))
    except Exception:
        now = now or datetime.now()
    if now.hour < QUIET_START:
        return False
    if now.hour > QUIET_END_H or (now.hour == QUIET_END_H and now.minute > QUIET_END_M):
        return False
    return True


def _is_permanent_failure(msg: str) -> bool:
    return any((msg or "").startswith(p) for p in _PERMANENT_FAIL_PREFIXES)


def _sms_reminder_already_sent(job: dict) -> bool:
    """Dedup rail: αν το 4h SMS reminder στάλθηκε ήδη για αυτό το booking,
    μην κάνεις ΚΑΙ voice no_show_reminder (διπλό όχλημα)."""
    if job.get("job_type") != "no_show_reminder" or not job.get("booking_id"):
        return False
    try:
        with db._conn() as c:
            row = c.execute("SELECT reminder_4 FROM bookings WHERE id=?",
                            (job["booking_id"],)).fetchone()
        return bool(row and row["reminder_4"])
    except Exception:
        return False


def _mark_reminder_sent(job: dict):
    """Μετά από επιτυχές voice no_show_reminder, σήκωσε το reminder_4 flag
    ώστε το check_reminders SMS να ΜΗΝ ξαναστείλει (ένα κανάλι ανά reminder)."""
    if job.get("job_type") != "no_show_reminder" or not job.get("booking_id"):
        return
    try:
        with db._conn() as c:
            c.execute("UPDATE bookings SET reminder_4=1 WHERE id=?", (job["booking_id"],))
            c.commit()
    except Exception:
        pass


_JOB_LABEL_GR = {
    "no_show_recovery":   "χαμένο ραντεβού (πρόταση επανακράτησης)",
    "no_show_reminder":   "υπενθύμιση ραντεβού",
    "win_back":           "επανενεργοποίηση παλιού ασθενή",
    "review_request":     "αίτημα αξιολόγησης",
    "pre_arrival_upsell": "ενημέρωση πριν το ραντεβού",
    "owner_briefing":     "ημερήσια ενημέρωση γιατρού",
}


def _alert_owner_unreachable(job: dict, reason: str):
    """Phone-only ασθενής χωρίς κανάλι (χωρίς SMS/voice/email) → ειδοποίησε τον ιδιοκτήτη
    να επικοινωνήσει χειροκίνητα, ώστε το recovery/win-back να ΜΗΝ χάνεται σιωπηλά."""
    try:
        from email_service import send_owner_notification
        what = _JOB_LABEL_GR.get(job.get("job_type"), job.get("job_type", ""))
        send_owner_notification(job["slug"], {
            "type": "outbound_unreachable",
            "customer": job.get("customer_name", "") or job.get("customer_phone", ""),
            "detail": (f"Δεν υπάρχει κανάλι (email/SMS) για «{what}» στον/στην "
                       f"{job.get('customer_name') or job.get('customer_phone')} "
                       f"({job.get('customer_phone')}). Κάλεσέ τον/την τηλεφωνικά."),
        })
    except Exception as e:
        print(f"[OUTBOUND] owner-unreachable alert failed: {e}")


# ── Executor (called by APScheduler every 5 min) ──────────────────────

def process_due_jobs(max_jobs: int = 10) -> dict:
    """Main scheduler entry point. Runs due jobs με P5 rails:
    quiet hours, daily cap/business, retry policy, channel dedup."""
    stats = {"total": 0, "done": 0, "failed": 0, "retried": 0, "skipped": 0}

    # Rail 1: quiet hours — έξω από 09:00-20:30 Athens δεν αγγίζουμε πελάτες.
    # Τα jobs μένουν pending και τρέχουν στο επόμενο επιτρεπτό window.
    if not _within_contact_hours():
        return stats

    jobs = db.get_due_outbound_jobs(limit=max_jobs)
    stats["total"] = len(jobs)
    if not jobs:
        return stats

    cap_hit = set()   # slugs που έπιασαν το daily cap σε αυτό το run
    cap_count = {}    # slug -> done-today counter (incremented as we go)

    for job in jobs:
        try:
            slug = job["slug"]

            # Rail 2: daily cap ανά business (cost-explosion guard).
            # owner_briefing ΕΞΑΙΡΕΙΤΑΙ: είναι εσωτερική ενημέρωση στον γιατρό (1/μέρα),
            # όχι customer call — δεν πρέπει να χάνεται επειδή γέμισε το cap πελατών.
            if job["job_type"] != "owner_briefing":
                if slug not in cap_count:
                    cap_count[slug] = db.count_outbound_done_today(slug)
                if cap_count[slug] >= DAILY_CAP_PER_BUSINESS:
                    if slug not in cap_hit:
                        print(f"[OUTBOUND] daily cap {DAILY_CAP_PER_BUSINESS} hit for {slug} — deferring jobs")
                        cap_hit.add(slug)
                    stats["skipped"] += 1
                    continue  # μένει pending → αύριο

            if job["attempts"] >= 2:
                db.mark_outbound_job(job["id"], "failed", "max_attempts_exceeded")
                stats["failed"] += 1
                continue

            # Rail 3b: CONSENT GATE (Ν.3471/2006 / GDPR) — choke point για ΟΛΟΥΣ τους παραγωγούς.
            # Οι αυτόνομες ενέργειες ανάκτησης/win-back στέλνονται ΜΟΝΟ σε ασθενείς με ρητή
            # καταγεγραμμένη συναίνεση επικοινωνίας. Έτσι κανένας παραγωγός (autonomous_loop,
            # auto_infer_no_shows, owner-tap) δεν κάνει auto-blast σε μη-συναινούντα. Τα transactional
            # (reminder για δικό του ραντεβού, owner_briefing) δεν περνούν από εδώ.
            if job["job_type"] in ("no_show_recovery", "win_back"):
                _cons = db.get_consent_status(job.get("customer_phone", ""), consent_type="reminders")
                if not (_cons and _cons.get("accepted")):
                    db.mark_outbound_job(job["id"], "done", "skipped_no_consent")
                    stats["skipped"] += 1
                    continue

            # Rail 4: dedup — ένα κανάλι ανά reminder
            if _sms_reminder_already_sent(job):
                db.mark_outbound_job(job["id"], "done", "skipped_sms_reminder_already_sent")
                stats["skipped"] += 1
                continue

            if job["channel"] == "voice":
                ok, msg = _execute_voice_job(job)
                # Voice νεκρό (no Vapi/agent — π.χ. χωρίς Twilio-attached αριθμό) αλλά
                # υπάρχει email → email fallback (ξεκλειδώνει win_back/upsell σε email).
                if not ok and job.get("customer_email") and msg.startswith(
                        ("vapi_not_configured", "no_voice_agent_provisioned", "voice_agent_missing_ids")):
                    ok, msg = _execute_email_job(job)
                    if ok:
                        msg = "voice_unavailable_email_sent"
            elif job["channel"] == "sms":
                ok, msg = _execute_sms_job(job)
                # Recovery moat: SMS νεκρό (κάθε αποτυχία) αλλά υπάρχει email → email fallback
                if not ok and job.get("customer_email"):
                    ok2, msg2 = _execute_email_job(job)
                    if ok2:
                        ok, msg = True, "sms_unavailable_email_sent"
            elif job["channel"] == "email":
                ok, msg = _execute_email_job(job)
            else:
                ok, msg = False, f"unknown_channel:{job['channel']}"

            if ok:
                db.mark_outbound_job(job["id"], "done", msg)
                _mark_reminder_sent(job)
                cap_count[slug] = cap_count.get(slug, 0) + 1  # owner_briefing μπορεί να μην έχει αρχικοποιηθεί
                stats["done"] += 1
            elif not job.get("customer_email") and msg.startswith(
                    ("twilio_not_configured", "vapi_not_configured",
                     "no_voice_agent_provisioned", "voice_agent_missing_ids")):
                # Κανένα κανάλι για να φτάσουμε τον ασθενή (phone-only, χωρίς SMS/voice).
                # ΜΗΝ σπαταλάς retries & ΜΗΝ χαθεί σιωπηλά → ειδοποίησε τον ιδιοκτήτη.
                _alert_owner_unreachable(job, msg)
                db.mark_outbound_job(job["id"], "failed", f"no_reachable_channel:{msg}")
                stats["failed"] += 1
            elif _is_permanent_failure(msg):
                db.mark_outbound_job(job["id"], "failed", msg)
                stats["failed"] += 1
            else:
                # Rail 3: transient failure → retry +30' (max 2 attempts συνολικά)
                if job["attempts"] + 1 >= 2:
                    db.mark_outbound_job(job["id"], "failed", f"gave_up: {msg}")
                    stats["failed"] += 1
                else:
                    db.retry_outbound_job(job["id"], delay_minutes=30)
                    stats["retried"] += 1
        except Exception as e:
            db.mark_outbound_job(job["id"], "failed", f"exception: {e}")
            stats["failed"] += 1

    return stats


def _execute_voice_job(job: dict) -> tuple[bool, str]:
    """Trigger an outbound voice call via Vapi."""
    if not vapi_client.is_configured():
        return False, "vapi_not_configured"

    business = db.get_business(job["slug"])
    if not business:
        return False, "business_not_found"

    # Look up Vapi assistant + phone_number for this business
    voice_agent = db.get_voice_agent(business["slug"]) if hasattr(db, "get_voice_agent") else None
    if not voice_agent:
        return False, "no_voice_agent_provisioned"

    assistant_id = voice_agent.get("vapi_assistant_id")
    phone_number_id = voice_agent.get("vapi_phone_number_id")
    if not assistant_id or not phone_number_id:
        return False, "voice_agent_missing_ids"

    # Build script
    payload = json.loads(job.get("payload_json") or "{}")

    # owner_briefing: ο AI ΚΑΛΕΙ τον γιατρό & διαβάζει τη σύνοψη (script = έτοιμο κείμενο)
    if job["job_type"] == "owner_briefing":
        script = payload.get("briefing_text", "") or "Καλημέρα, ενημέρωση από το ιατρείο σου."
        try:
            # firstMessage = η σύνοψη → ο assistant ΤΗΝ ΔΙΑΒΑΖΕΙ στον γιατρό (όχι default prompt)
            result = vapi_client.create_outbound_call(
                phone_number_id=phone_number_id, assistant_id=assistant_id,
                to_number=job["customer_phone"], customer_name=job["customer_name"],
                first_message=script)
            call_id = result.get("id") or result.get("call", {}).get("id", "")
            return True, f"vapi_call_id={call_id} script=owner_briefing"
        except vapi_client.VapiError as e:
            return False, f"vapi_error: {e}"

    script_fn = SCRIPT_BUILDERS.get(job["job_type"])
    if not script_fn:
        return False, f"no_script_for_{job['job_type']}"

    if job["job_type"] in ("pre_arrival_upsell", "no_show_reminder"):
        script = script_fn(
            business_name=business["name"],
            customer_name=job["customer_name"],
            date_str=payload.get("booking_date", ""),
            time_str=payload.get("booking_time", ""),
            service=payload.get("service", "ραντεβού"),
        )
    elif job["job_type"] == "review_request":
        script = script_fn(
            business_name=business["name"],
            customer_name=job["customer_name"],
            review_url=business.get("google_review_url", "") or "",
        )
    elif job["job_type"] == "win_back":
        script = script_fn(
            business_name=business["name"],
            customer_name=job["customer_name"],
            last_visit=payload.get("last_visit", ""),
        )
    else:
        return False, "unknown_job_type"

    # Trigger Vapi outbound call.
    # NOTE: Vapi's create_outbound_call uses the assistant's default prompt.
    # The custom script is logged as call notes; assistant uses assistant-request
    # webhook to inject memory + script at runtime (see voice/webhook_handler.py).
    try:
        result = vapi_client.create_outbound_call(
            phone_number_id=phone_number_id,
            assistant_id=assistant_id,
            to_number=job["customer_phone"],
            customer_name=job["customer_name"],
        )
        call_id = result.get("id") or result.get("call", {}).get("id", "")
        return True, f"vapi_call_id={call_id} script={job['job_type']}"
    except vapi_client.VapiError as e:
        return False, f"vapi_error: {e}"


def _execute_sms_job(job: dict) -> tuple[bool, str]:
    """Fallback SMS for jobs where voice isn't appropriate."""
    if not twilio_client.is_configured():
        return False, "twilio_not_configured"

    business = db.get_business(job["slug"])
    if not business:
        return False, "business_not_found"

    payload = json.loads(job.get("payload_json") or "{}")
    name = (job.get("customer_name") or "").strip() or "πελάτη"
    biz = business.get("name", "")

    if job["job_type"] == "no_show_reminder":
        body = (f"{biz}: Υπενθύμιση για το ραντεβού σας σήμερα στις "
                f"{payload.get('booking_time','')}. Επιβεβαιώστε με 'NAI' "
                f"ή απαντήστε για αλλαγή. Ευχαριστούμε!")
    elif job["job_type"] == "review_request":
        url = business.get("google_review_url", "")
        body = (f"{biz}: Ελπίζουμε να σας ευχαριστήσαμε χθες, {name}! "
                f"Aν θέλετε, μια κριτική στο Google μας βοηθάει πολύ: "
                f"{url or '(link σύντομα)'}")
    elif job["job_type"] == "win_back":
        body = (f"{biz}: {name}, σας θυμηθήκαμε! 15% για επανερχόμενους — "
                f"απαντήστε για ραντεβού.")
    elif job["job_type"] == "no_show_recovery":
        url = payload.get("rebook_url", "")
        if url:
            body = (f"{biz}: {name}, λυπούμαστε που δεν τα καταφέρατε στο ραντεβού σας! "
                    f"Θέλετε να ξανακλείσουμε; Κλείστε εδώ: {url}")
        else:
            body = (f"{biz}: {name}, λυπούμαστε που δεν τα καταφέρατε στο ραντεβού σας! "
                    f"Απαντήστε εδώ για νέο ραντεβού.")
    else:
        body = f"{biz}: Σας ευχαριστούμε. — Aυτόματο μήνυμα"

    try:
        twilio_client.send_sms(job["customer_phone"], body[:300])
        return True, "sms_sent"
    except Exception as e:
        return False, f"sms_error: {e}"


def _execute_email_job(job: dict) -> tuple[bool, str]:
    """Email path (fallback ή primary) via Resend. Καλύπτει & no_show_recovery (το moat)."""
    if not job.get("customer_email"):
        return False, "no_email"
    try:
        from email_service import send_email_via_resend
        business = db.get_business(job["slug"]) or {}
        biz = business.get("name", "το ιατρείο")
        name = (job.get("customer_name") or "").strip() or "σας"
        payload = json.loads(job.get("payload_json") or "{}")
        url = payload.get("rebook_url", "")
        jt = job["job_type"]
        # owner_briefing: έτοιμο rich email (voice→email fallback της ημερήσιας ενημέρωσης)
        if jt == "owner_briefing":
            html = payload.get("html") or f"<p>{payload.get('briefing_text','')}</p>"
            subject = payload.get("subject") or "Ημερήσια ενημέρωση"
            send_email_via_resend(job["customer_email"], subject, html,
                                  from_name=biz)
            return True, "email_sent"
        subject = {
            "no_show_reminder":   "Υπενθύμιση για το ραντεβού σας",
            "no_show_recovery":   "Χάσαμε το ραντεβού σας — να ξανακλείσουμε;",
            "review_request":     "Πώς ήταν η εμπειρία σας;",
            "win_back":           "Σας θυμηθήκαμε",
            "pre_arrival_upsell": "Αναβάθμιση πριν την επίσκεψή σας;",
        }.get(jt, "Από το ραντεβού σας")
        if jt == "no_show_recovery":
            cta = (f'<p><a href="{url}" style="background:#2563eb;color:#fff;padding:10px 18px;'
                   f'border-radius:8px;text-decoration:none">Κλείσε νέο ραντεβού</a></p>') if url else ""
            body = (f'<div style="font-family:-apple-system,Segoe UI,sans-serif;color:#0f172a">'
                    f'<p>Καλημέρα {name},</p><p>Λυπούμαστε που δεν τα καταφέρατε στο ραντεβού σας στο '
                    f'<b>{biz}</b>. Θα θέλατε να ξανακλείσουμε;</p>{cta}'
                    f'<p style="color:#64748b;font-size:13px">— {biz}</p></div>')
        elif jt == "win_back":
            rb = url or payload.get("rebook_url", "")
            cta = (f'<p><a href="{rb}" style="background:#2563eb;color:#fff;padding:10px 18px;'
                   f'border-radius:8px;text-decoration:none">Κλείσε ραντεβού</a></p>') if rb else ""
            body = (f'<div style="font-family:-apple-system,Segoe UI,sans-serif;color:#0f172a">'
                    f'<p>Καλημέρα {name},</p><p>Έχει καιρό να σας δούμε στο <b>{biz}</b> — σας θυμηθήκαμε! '
                    f'Έχουμε <b>15% έκπτωση</b> για επανερχόμενους. Θα χαρούμε να σας ξαναδούμε.</p>{cta}'
                    f'<p style="color:#64748b;font-size:13px">— {biz}</p></div>')
        else:
            body = (f'<div style="font-family:-apple-system,Segoe UI,sans-serif;color:#0f172a">'
                    f'<p>Καλημέρα {name},</p><p>Αυτόματο μήνυμα από το <b>{biz}</b>. '
                    f'Απαντήστε σε αυτό το email αν έχετε ερώτηση.</p></div>')
        send_email_via_resend(job["customer_email"], subject, body)
        return True, "email_sent"
    except Exception as e:
        return False, f"email_error: {e}"


# ── Manual triggers (admin UI / CLI / testing) ────────────────────────

def enqueue_winback_for_inactive_customers(slug: str, days_inactive: int = 90,
                                            limit: int = 10, cooldown_days: int = 60) -> int:
    """Find customers with no booking in N days and enqueue win-back calls.
    cooldown_days: skip phones που έχουν ήδη πάρει win_back τις τελευταίες N μέρες
    (αλλιώς το weekly cron θα σπαμάρει τον ίδιο ανενεργό κάθε βδομάδα)."""
    cutoff = (datetime.now() - timedelta(days=days_inactive)).strftime("%Y-%m-%d")
    cooldown_cutoff = (datetime.now() - timedelta(days=cooldown_days)).strftime("%Y-%m-%d %H:%M:%S")
    count = 0
    with db._conn() as conn:
        rows = conn.execute("""
            SELECT phone, MAX(name) as name, MAX(email) as email, MAX(date) as last_date
            FROM bookings
            WHERE slug=? AND status IN ('confirmed','completed')
            GROUP BY phone
            HAVING last_date <= ?
            ORDER BY last_date DESC
            LIMIT ?
        """, (slug, cutoff, limit)).fetchall()
        # Cooldown: phones που πήραν ήδη win_back πρόσφατα — μην ξανακαλέσεις
        recent = conn.execute("""
            SELECT DISTINCT customer_phone FROM outbound_jobs
            WHERE slug=? AND job_type='win_back' AND scheduled_at > ?
        """, (slug, cooldown_cutoff)).fetchall()
        recent_phones = {r["customer_phone"] for r in recent}

    for r in rows:
        if r["phone"] in recent_phones:
            continue
        scheduled = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        db.enqueue_outbound_job(
            slug=slug,
            job_type="win_back",
            customer_phone=r["phone"],
            customer_name=r["name"] or "",
            customer_email=r["email"] or "",
            scheduled_at=scheduled,
            channel="voice",
            payload={"last_visit": r["last_date"]},
            dedup=False,  # no booking_id to dedup against — cooldown above handles repeats
        )
        count += 1
    return count
