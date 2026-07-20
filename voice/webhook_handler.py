"""
Vapi webhook handler.

Vapi στέλνει events στο serverUrl:
1. function-call         — AI θέλει να καλέσει μια function (book_appointment κλπ)
2. end-of-call-report    — κλήση τελείωσε, εδώ είναι το transcript + analysis
3. status-update         — κατάσταση κλήσης άλλαξε (ringing, in-progress, ended)
4. transfer-destination  — AI ζήτησε transfer, εμείς λέμε σε ποιο νούμερο
5. assistant-request     — Vapi ρωτά για enriched assistant config πριν την κλήση
                           (εδώ injectάρουμε Memory Layer για repeat callers)

Κάθε function-call απαντάμε ΓΡΗΓΟΡΑ (< 2 sec) με JSON {"result": "..."}.
Vapi το δίνει στο AI ως tool result → AI απαντάει στον πελάτη.

WAVE-AHEAD: Memory Layer integration (Feature #1, 2026-05-28)
  - Pre-call: injects memory into system prompt για repeat callers
  - On-booking: stores caller phone + name + service in customer_memory
  - End-of-call: extracts facts from transcript, stores back to memory
"""
import os
import json
import re
import threading
from datetime import datetime, date, timedelta
from typing import Optional
import database as db


def _log_call_event(business_slug: str, call_id: str, event_type: str,
                    payload: dict, response: dict = None):
    """Audit log για debugging."""
    try:
        with db._conn() as c:
            c.execute("""
                INSERT INTO voice_call_events
                    (business_slug, vapi_call_id, event_type,
                     payload_json, response_json)
                VALUES (?,?,?,?,?)
            """, (
                business_slug, call_id, event_type,
                json.dumps(payload, ensure_ascii=False)[:50000],
                json.dumps(response, ensure_ascii=False)[:10000] if response else None,
            ))
            c.commit()
    except Exception as e:
        print(f"[voice-webhook] log failed: {e}")


# ── Function call handlers ─────────────────────────────────────────────

def _handle_check_availability(business_slug: str, params: dict) -> dict:
    """Returns available time slots for a date."""
    target_date = params.get("date", "")
    service = params.get("service", "general")

    business = db.get_business(business_slug)
    if not business:
        return {"result": "Συγγνώμη, υπάρχει τεχνικό πρόβλημα. Δοκιμάστε αργότερα."}

    # Default duration based on service hint
    duration = 30
    services = business.get("services", [])
    if isinstance(services, str):
        try:
            services = json.loads(services)
        except Exception:
            services = []
    for s in (services or []):
        if isinstance(s, dict) and service.lower() in (s.get("name", "")).lower():
            duration = int(s.get("duration", 30))
            break

    try:
        day_start, day_end, is_closed = db.get_day_hours(business, target_date)
        if is_closed:
            return {"result": f"Είμαστε κλειστά την {_format_date_greek(target_date)}. Θέλετε να δούμε άλλη μέρα;"}
        slots = db.get_available_slots(
            business_slug, target_date, duration,
            day_start, day_end, business.get("slot_minutes", 30),
            closed_days=business.get("closed_days", []),
            blocked_dates=business.get("blocked_dates", []),
        )
    except Exception as e:
        print(f"[voice-webhook] check_availability error: {e}")
        return {"result": "Δεν μπόρεσα να δω το πρόγραμμα αυτή τη στιγμή. Ας δοκιμάσουμε ξανά σε λίγο;"}

    if not slots:
        # Try next available date
        return {
            "result": f"Δεν έχει διαθέσιμη ώρα την {_format_date_greek(target_date)}. "
                      "Θέλετε να δοκιμάσουμε άλλη μέρα;"
        }

    # Format slots for voice
    if len(slots) <= 3:
        slots_text = ", ".join(_format_time_greek(s) for s in slots)
        return {"result": f"Έχω διαθέσιμα: {slots_text}. Ποιο σας βολεύει;"}

    first3 = ", ".join(_format_time_greek(s) for s in slots[:3])
    return {"result": f"Έχω αρκετές ώρες — π.χ. {first3}, και άλλες. Τι ώρα προτιμάτε;"}


def _handle_book_appointment(business_slug: str, params: dict) -> dict:
    """Creates the actual booking. Also writes to customer_memory (Feature #1)."""
    business = db.get_business(business_slug)
    if not business:
        return {"result": "Συγγνώμη, τεχνικό πρόβλημα."}

    try:
        # Default duration if not specified
        duration = 30
        services = business.get("services", [])
        if isinstance(services, str):
            try:
                services = json.loads(services)
            except Exception:
                services = []
        service_name = params.get("service", "Ραντεβού")
        for s in (services or []):
            if isinstance(s, dict) and service_name.lower() in (s.get("name", "")).lower():
                duration = int(s.get("duration", 30))
                service_name = s.get("name", service_name)
                break

        booking_id = db.create_booking(
            slug=business_slug,
            date=params["date"],
            time=params["time"],
            service=service_name,
            duration=duration,
            name=params["customer_name"],
            phone=params["customer_phone"],
            email=params.get("email", ""),
            notes=f"[VOICE] {params.get('notes', '')}",
        )

        # ── ΣΥΝΔΕΣΗ ΜΕ BACK OFFICE: φάκελος ασθενή + graph link ──────────────
        # Χωρίς αυτό, η φωνητική κράτηση φαινόταν στο «Ημέρα» (bookings) αλλά ΟΧΙ στα
        # «Φάκελοι» (bo_patients) μέχρι το nightly sync. Τώρα = άμεσα ορατό & συνδεδεμένο.
        try:
            import back_office as _bo
            _bo.upsert_patient(business_slug, params["customer_phone"],
                               params["customer_name"], params.get("email", ""))
            _bo.link(business_slug, "patient", params["customer_phone"],
                     "booking", booking_id, "appointment")
        except Exception as bo_err:
            print(f"[voice-webhook] back-office patient link skipped: {bo_err}")

        # Memory Layer (Feature #1): store caller for future personalization
        try:
            db.upsert_customer_memory(
                slug=business_slug,
                identifier=params["customer_phone"],
                identifier_type="phone",
                display_name=params["customer_name"],
                preferred_service=service_name,
                last_topics=f"booking για {service_name}",
            )
        except Exception as mem_err:
            print(f"[voice-webhook] memory upsert skipped: {mem_err}")

        # ── ΚΡΙΣΙΜΟ FIX (voice booking fail): SMS + owner-email = network calls.
        #    Synchronously μπλόκαραν το tool response → το Vapi έπαιρνε
        #    "No result returned" → η AI έλεγε «υπάρχει τεχνικό πρόβλημα με το
        #    κλείσιμο». Τώρα τρέχουν σε background thread — η φωνή απαντάει ΑΜΕΣΩΣ.
        _p = dict(params)  # snapshot για το thread (το params μπορεί να αλλάξει)
        _biz_name = business.get("name", "")
        def _notify_async():
            # SMS confirmation (αν Twilio configured)
            try:
                from voice.twilio_client import send_sms, is_configured as twilio_ok
                if twilio_ok():
                    sms_body = (
                        f"{_biz_name}: Το ραντεβού σας επιβεβαιώθηκε για "
                        f"{_format_date_greek(_p['date'])} στις "
                        f"{_format_time_greek(_p['time'])}. Ευχαριστούμε!"
                    )
                    send_sms(_p["customer_phone"], sms_body)
            except Exception as sms_err:
                print(f"[voice-webhook] SMS confirmation skipped: {sms_err}")
            # Owner notification (email)
            try:
                from email_service import send_owner_notification
                send_owner_notification(business_slug, {
                    "type": "voice_booking",
                    "customer": _p["customer_name"],
                    "phone": _p["customer_phone"],
                    "date": _p["date"],
                    "time": _p["time"],
                    "service": service_name,
                })
            except Exception as own_err:
                print(f"[voice-webhook] owner notification failed: {own_err}")
        try:
            threading.Thread(target=_notify_async, daemon=True).start()
        except Exception as _te:
            print(f"[voice-webhook] notify thread failed: {_te}")

        # ΜΗΝ υπόσχεσαι SMS αν δεν είναι configured (αλλιώς λέμε ψέματα στον ασθενή)
        try:
            from voice.twilio_client import is_configured as _tw_ok
            _has_sms = _tw_ok()
        except Exception:
            _has_sms = False
        _confirm = "Θα λάβετε SMS επιβεβαίωσης σε λίγο." if _has_sms else "Σας περιμένουμε!"
        return {
            "result": (
                f"Τέλεια! Έκλεισα το ραντεβού σας για "
                f"{_format_date_greek(params['date'])} στις "
                f"{_format_time_greek(params['time'])}. {_confirm}"
            )
        }

    except ValueError as e:
        msg = str(e)
        if "double_booking" in msg:
            return {
                "result": "Συγγνώμη, αυτή την ώρα κάποιος άλλος μόλις την έκλεισε. "
                          "Ας δούμε άλλη ώρα;"
            }
        if "past_date" in msg or "invalid_datetime" in msg:
            return {"result": "Αυτή η ώρα δεν είναι διαθέσιμη. Θέλετε να δούμε μια άλλη μέρα ή ώρα;"}
        print(f"[voice-webhook] book_appointment ValueError: {e}")
        return {"result": "Δεν μπόρεσα να κλείσω αυτό το ραντεβού. Ας δοκιμάσουμε μια άλλη ώρα;"}
    except Exception as e:
        print(f"[voice-webhook] book_appointment error: {e}")
        return {"result": "Είχα ένα τεχνικό πρόβλημα. Μπορώ να δοκιμάσω ξανά;"}


def _handle_transfer_to_owner(business_slug: str, params: dict) -> dict:
    """Returns transfer destination για Vapi."""
    business = db.get_business(business_slug)
    if not business:
        return {"result": "Συγγνώμη, δεν μπορώ να σας μεταφέρω αυτή τη στιγμή."}

    owner_phone = business.get("phone", "")
    if not owner_phone:
        return {
            "result": (
                "Δε μπορώ να σας μεταφέρω αυτή τη στιγμή. Θα ζητήσω από τον "
                "ιδιοκτήτη να σας πάρει σύντομα. Πείτε μου το τηλέφωνό σας;"
            )
        }

    # Vapi expects 'destination' field for transfer
    return {
        "destination": {
            "type": "number",
            "number": owner_phone,
            "message": "Σας περνάω αμέσως, μη κλείσετε.",
        },
        "result": f"Σας μεταφέρω στον υπεύθυνο.",
    }


def _handle_send_sms_confirmation(business_slug: str, params: dict) -> dict:
    """Manual SMS trigger (κανονικά γίνεται αυτόματα στο book_appointment)."""
    try:
        from voice.twilio_client import send_sms, is_configured as twilio_ok
        if not twilio_ok():
            return {"result": "SMS δεν διαμορφώθηκε."}
        send_sms(params["customer_phone"], params["message"][:300])
        return {"result": "SMS στάλθηκε."}
    except Exception as e:
        print(f"[voice-webhook] sms send failed: {e}")
        return {"result": "Δεν στάλθηκε το SMS, αλλά το ραντεβού σας είναι κλεισμένο."}


def _handle_lookup_appointment(business_slug: str, params: dict) -> dict:
    """Βρίσκει τα ΕΠΕΡΧΟΜΕΝΑ ραντεβού ενός ασθενή (με βάση το τηλέφωνο) — η φωνή «βλέπει»
    τι έχει κλειστό, συνδεδεμένη με το ίδιο back office."""
    phone = (params.get("customer_phone") or "").strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 6:
        return {"result": "Πείτε μου το τηλέφωνό σας να βρω το ραντεβού σας."}
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with db._conn() as c:
            rows = c.execute(
                "SELECT date, time, service FROM bookings WHERE slug=? AND status='confirmed' "
                "AND REPLACE(REPLACE(phone,'+',''),' ','') LIKE ? AND date>=? "
                "ORDER BY date, time LIMIT 3",
                (business_slug, "%" + digits[-9:], today)).fetchall()
    except Exception as e:
        return {"result": f"Δεν μπόρεσα να ελέγξω αυτή τη στιγμή."}
    if not rows:
        return {"result": "Δεν βρήκα επερχόμενο ραντεβού στο τηλέφωνό σας. Θέλετε να κλείσουμε ένα;"}
    parts = [f"{_format_date_greek(r['date'])} στις {_format_time_greek(r['time'])}"
             + (f" για {r['service']}" if r["service"] else "") for r in rows]
    if len(parts) == 1:
        return {"result": f"Το ραντεβού σας είναι {parts[0]}. Θέλετε κάτι άλλο;"}
    return {"result": "Έχετε: " + " · ".join(parts) + ". Θέλετε κάτι άλλο;"}


def _handle_add_to_waitlist(business_slug: str, params: dict) -> dict:
    """Βάζει τον καλούντα στη ΛΙΣΤΑ ΑΝΑΜΟΝΗΣ για συγκεκριμένη μέρα/ώρα (ή προτιμώμενη).
    Όταν κάποιος ακυρώσει ΑΥΤΟ το slot, ο πρώτος της αναμονής τραβιέται αυτόματα
    (notify_waitlist → claim link + SMS). Έτσι η χαμένη κλήση γίνεται εξασφαλισμένο ραντεβού."""
    business = db.get_business(business_slug)
    if not business:
        return {"result": "Συγγνώμη, τεχνικό πρόβλημα."}
    try:
        date = params.get("date", "")
        time = params.get("time", "") or "any"
        service = params.get("service", "Ραντεβού")
        name = params.get("customer_name", "")
        phone = params.get("customer_phone", "")
        if not (date and name and phone):
            return {"result": "Χρειάζομαι ημερομηνία, όνομα και τηλέφωνο για τη λίστα αναμονής."}
        wid = db.add_to_waitlist(business_slug, date, time, service, name, phone,
                                 params.get("email", ""))
        # ίδια σύνδεση με back-office όπως στο booking → άμεσα ορατός ο ασθενής
        try:
            import back_office as _bo
            _bo.upsert_patient(business_slug, phone, name, params.get("email", ""))
        except Exception as bo_err:
            print(f"[voice-webhook] waitlist back-office link skipped: {bo_err}")
        when = f"στις {time} " if time and time != "any" else ""
        return {"result": (f"Σας κατέγραψα στη λίστα αναμονής για {date} {when}({service}). "
                           f"Μόλις ελευθερωθεί θέση, θα ειδοποιηθείτε αμέσως με SMS. "
                           f"Αριθμός αναμονής: {wid}. Θέλετε κάτι άλλο;")}
    except Exception as e:
        print(f"[voice-webhook] add_to_waitlist error: {e}")
        return {"result": "Συγγνώμη, δεν μπόρεσα να σας καταχωρήσω στη λίστα. Δοκιμάστε ξανά."}


FUNCTION_HANDLERS = {
    "check_availability": _handle_check_availability,
    "book_appointment": _handle_book_appointment,
    "lookup_appointment": _handle_lookup_appointment,
    "add_to_waitlist": _handle_add_to_waitlist,
    "transfer_to_owner": _handle_transfer_to_owner,
    "send_sms_confirmation": _handle_send_sms_confirmation,
}


# ── End-of-call: save full call record ─────────────────────────────────

def _handle_end_of_call(business_slug: str, payload: dict) -> dict:
    """Vapi στέλνει transcript, recording URL, analysis, cost. Σώζουμε όλα.
    Plus (Feature #1): extract memory facts from transcript via memory_service."""
    call_obj = payload.get("call", {}) or {}
    artifact = payload.get("artifact", {}) or {}
    analysis = payload.get("analysis", {}) or {}
    cost = payload.get("cost", 0)
    duration_sec = payload.get("durationSeconds", 0)

    caller_phone = (call_obj.get("customer") or {}).get("number") or ""

    try:
        with db._conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO voice_calls
                    (business_slug, vapi_call_id, twilio_call_sid,
                     from_number, to_number, started_at, ended_at,
                     duration_seconds, cost_usd, transcript, summary,
                     structured_data_json, success_evaluation,
                     recording_url, ended_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                business_slug,
                call_obj.get("id"),
                call_obj.get("phoneCallProviderId"),
                caller_phone,
                call_obj.get("phoneNumber", {}).get("number"),
                call_obj.get("startedAt"),
                call_obj.get("endedAt"),
                int(duration_sec or 0),
                float(cost or 0),
                _enc((artifact.get("transcript") or "")[:50000]),
                _enc((analysis.get("summary") or "")[:5000]),
                # structuredData μπορεί να έχει εξαγμένα PII (όνομα/τηλ/ραντεβού) → encrypted at rest
                _enc(json.dumps(analysis.get("structuredData") or {}, ensure_ascii=False)),
                (analysis.get("successEvaluation") or "")[:500],
                artifact.get("recordingUrl"),
                call_obj.get("endedReason"),
            ))
            c.commit()
    except Exception as e:
        print(f"[voice-webhook] save call failed: {e}")

    # Memory Layer (Feature #1): extract facts from voice transcript
    if caller_phone:
        transcript = artifact.get("transcript") or ""
        if transcript and len(transcript) > 50:
            def _extract_and_store():
                try:
                    from memory_service import extract_facts_from_conversation
                    # Convert transcript text into messages list
                    msgs = _transcript_to_messages(transcript)
                    facts = extract_facts_from_conversation(msgs)
                    if facts:
                        facts["identifier_type"] = "phone"
                        db.upsert_customer_memory(business_slug, caller_phone, **facts)
                        try:
                            db.log_audit(action="voice_memory_write",
                                         slug=business_slug,
                                         customer_id=caller_phone,
                                         details=f"keys={list(facts.keys())}")
                        except Exception:
                            pass
                except Exception as e:
                    print(f"[voice-webhook] memory extract failed: {e}")
            threading.Thread(target=_extract_and_store, daemon=True).start()

    return {"received": True}


def _transcript_to_messages(transcript: str) -> list:
    """Vapi transcript = 'AI: ...\\nUser: ...\\n'. Convert to messages list."""
    msgs = []
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith(("ai:", "assistant:", "bot:", "agent:")):
            content = line.split(":", 1)[1].strip()
            msgs.append({"role": "assistant", "content": content})
        elif lower.startswith(("user:", "customer:", "πελάτης:")):
            content = line.split(":", 1)[1].strip()
            msgs.append({"role": "user", "content": content})
    return msgs


# ── Assistant request: inject memory into system prompt (Feature #1) ──

def _handle_assistant_request(business_slug: str, payload: dict) -> dict:
    """Vapi calls this BEFORE the call. We return enriched assistant config
    with the caller's memory injected if they're a returning caller.

    Returns: {"assistant": {"model": {"systemPrompt": "..."}}} format expected by Vapi.
    """
    call_obj = payload.get("call", {}) or {}
    caller_phone = (call_obj.get("customer") or {}).get("number") or ""

    memory_snippet = ""
    if caller_phone:
        try:
            mem = db.get_customer_memory(business_slug, caller_phone)
            if mem:
                from memory_service import render_memory_section
                memory_snippet = render_memory_section(mem, lang="el")
        except Exception as e:
            print(f"[voice-webhook] memory fetch skipped: {e}")

    # Return enriched config — let Vapi merge with existing assistant
    if memory_snippet:
        return {
            "assistant": {
                "model": {
                    "messages": [{
                        "role": "system",
                        "content": memory_snippet,
                    }]
                }
            }
        }
    return {"assistant": {}}


# ── Main entry point ──────────────────────────────────────────────────

try:
    from crypto_util import encrypt as _enc, decrypt as _dec
except Exception:
    _enc = _dec = lambda x: x


def _run_tool(business_slug: str, name: str, params: dict, caller_phone: str = "") -> dict:
    """Εκτελεί ένα voice function-call. Κοινό για legacy 'function-call' & νέο 'tool-calls'."""
    params = params or {}
    # CALLER ID: η AI δεν ρωτάει τηλέφωνο — βάζουμε αυτό της κλήσης αν λείπει/είναι μικρό
    if caller_phone and name in ("book_appointment", "lookup_appointment"):
        cur = "".join(ch for ch in str(params.get("customer_phone", "")) if ch.isdigit())
        if len(cur) < 8:
            params["customer_phone"] = caller_phone
    handler = FUNCTION_HANDLERS.get(name)
    if not handler:
        return {"result": f"Δεν αναγνωρίζω τη λειτουργία {name}."}
    try:
        return handler(business_slug, params or {})
    except Exception as e:
        print(f"[voice-webhook] handler {name} crashed: {e}")
        return {"result": "Είχα ένα τεχνικό πρόβλημα."}


def handle_vapi_webhook(business_slug: str, payload: dict) -> dict:
    """Main router για όλα τα Vapi server messages."""
    message = payload.get("message", {}) or {}
    msg_type = message.get("type", "")
    call_id = (message.get("call") or {}).get("id", "")
    # CALLER ID: ο αριθμός που καλεί — για να μη ρωτάει τηλέφωνο & να θυμάται τον πελάτη
    caller_phone = ((message.get("call") or {}).get("customer") or {}).get("number") or ""

    _log_call_event(business_slug, call_id, msg_type, message)

    if msg_type == "function-call":
        # Legacy Vapi format
        fn = message.get("functionCall", {}) or {}
        return _run_tool(business_slug, fn.get("name", ""), fn.get("parameters", {}) or {}, caller_phone)

    elif msg_type in ("tool-calls", "tool_calls"):
        # Νέο Vapi format (model.tools): array toolCalls, response {"results":[{toolCallId,result}]}
        tcs = message.get("toolCalls") or message.get("toolCallList") or []
        results = []
        for tc in tcs:
            tc_id = tc.get("id") or tc.get("toolCallId") or ""
            f = tc.get("function", {}) or {}
            args = f.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except Exception:
                    args = {}
            r = _run_tool(business_slug, f.get("name", ""), args or {}, caller_phone)
            results.append({"toolCallId": tc_id,
                            "result": r.get("result", "") if isinstance(r, dict) else str(r)})
        return {"results": results}

    elif msg_type == "end-of-call-report":
        return _handle_end_of_call(business_slug, message)

    elif msg_type == "transfer-destination-request":
        return _handle_transfer_to_owner(business_slug, {})

    elif msg_type == "assistant-request":
        # Feature #1: Memory Layer — enrich system prompt με caller memory
        return _handle_assistant_request(business_slug, message)

    elif msg_type == "status-update":
        return {"received": True}

    return {"received": True, "type": msg_type}


# ── Formatting helpers για voice output ────────────────────────────────

GREEK_MONTHS = [
    "Ιανουαρίου", "Φεβρουαρίου", "Μαρτίου", "Απριλίου", "Μαΐου", "Ιουνίου",
    "Ιουλίου", "Αυγούστου", "Σεπτεμβρίου", "Οκτωβρίου", "Νοεμβρίου", "Δεκεμβρίου",
]
GREEK_DAYS = [
    "Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη",
    "Παρασκευή", "Σάββατο", "Κυριακή",
]


def _format_date_greek(date_str: str) -> str:
    """'2026-05-22' → 'Παρασκευή είκοσι δύο Μαΐου'"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return f"{GREEK_DAYS[d.weekday()]} {d.day} {GREEK_MONTHS[d.month-1]}"
    except Exception:
        return date_str


def _format_time_greek(time_str: str) -> str:
    """'21:00' → 'εννέα το βράδυ' (ΠΟΤΕ 24ωρη μορφή — η AI έλεγε «21»)."""
    _GR_NUM = {1: "μία", 2: "δύο", 3: "τρεις", 4: "τέσσερις", 5: "πέντε", 6: "έξι",
               7: "εφτά", 8: "οκτώ", 9: "εννέα", 10: "δέκα", 11: "έντεκα", 12: "δώδεκα"}
    try:
        h, m = time_str.split(":")
        h = int(h); m = int(m)
        h12 = h if 1 <= h <= 12 else (h - 12 if h > 12 else 12)
        word = _GR_NUM.get(h12, str(h12))
        if 5 <= h <= 11:
            period = " το πρωί"
        elif 12 <= h <= 16:
            period = " το μεσημέρι"
        elif 17 <= h <= 20:
            period = " το απόγευμα"
        else:
            period = " το βράδυ"
        if m == 0:
            return f"{word}{period}"
        if m == 30:
            return f"{word} και μισή{period}"
        return f"{word} και {m}{period}"
    except Exception:
        return time_str
