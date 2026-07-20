"""
routes/fhir_routes.py — Ran-Tevu FHIR R4 endpoints (per-slug, tenant-scoped).

  GET  /fhir/metadata                 → CapabilityStatement (public, standard discovery)
  GET  /fhir/<slug>/Patient           → Bundle των ασθενών του ιατρείου (owner-token gated)
  GET  /fhir/<slug>/Appointment       → Bundle των ραντεβού
  GET  /fhir/<slug>/Invoice           → Bundle των τιμολογίων
  POST /fhir/<slug>/Appointment       → INGEST εξωτερικού FHIR ραντεβού → πραγματική κράτηση
  GET  /api/<slug>/bridges/status     → τίμιος πίνακας διασυνδέσεων (LIVE / needs-key / no-api)

Read/write είναι tenant-scoped (owner token) → το ιατρείο βλέπει ΤΑ ΔΙΚΑ ΤΟΥ δεδομένα (ίδιος data
controller). Συνδέεται ΜΟΝΟ με συστήματα που μιλάνε FHIR· ΗΔΙΚΑ/Doctoranytime = χωρίς δημόσιο API.
"""
import os
from flask import Blueprint, request, jsonify

import database as db
import fhir_bridge as F

bp = Blueprint("fhir", __name__)


def _gate(slug):
    try:
        from routes.back_office_routes import _check_token
        return _check_token(slug)
    except Exception:
        return False


def _patients(slug):
    with db._conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM bo_patients WHERE slug=?", (slug,)).fetchall()]


def _invoices(slug):
    with db._conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM bo_invoices WHERE slug=?", (slug,)).fetchall()]


@bp.get("/fhir/metadata")
def fhir_metadata():
    """FHIR R4 discovery — public, όπως ορίζει το πρότυπο."""
    return jsonify(F.capability_statement())


@bp.get("/fhir/<slug>/Patient")
def fhir_patients(slug):
    if not _gate(slug):
        return jsonify({"error": "unauthorized"}), 403
    return jsonify(F.bundle([F.patient_to_fhir(p) for p in _patients(slug)]))


@bp.get("/fhir/<slug>/Appointment")
def fhir_appointments(slug):
    if not _gate(slug):
        return jsonify({"error": "unauthorized"}), 403
    return jsonify(F.bundle([F.booking_to_fhir(b) for b in db.get_all_bookings(slug)]))


@bp.get("/fhir/<slug>/Invoice")
def fhir_invoices(slug):
    if not _gate(slug):
        return jsonify({"error": "unauthorized"}), 403
    return jsonify(F.bundle([F.invoice_to_fhir(i) for i in _invoices(slug)]))


@bp.post("/fhir/<slug>/Appointment")
def fhir_create_appointment(slug):
    """INGEST: σύστημα που μιλάει FHIR στέλνει Appointment → πραγματική κράτηση στο Ran-Tevu."""
    if not _gate(slug):
        return jsonify({"error": "unauthorized"}), 403
    try:
        f = F.fhir_to_booking(request.get_json(silent=True, force=True) or {})
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    try:
        bid = db.create_booking(slug, f["date"], f["time"], f["service"], f["duration"],
                                f["name"], f["phone"], "", "[FHIR ingest]")
    except ValueError as e:
        if "double_booking" in str(e):
            return jsonify({"error": "slot_already_booked"}), 409
        return jsonify({"error": str(e)}), 400
    b = db.get_booking(bid)
    return jsonify(F.booking_to_fhir(b)), 201


@bp.get("/api/<slug>/bridges/status")
def bridges_status(slug):
    """Ποιες εξωτερικές γέφυρες είναι ζωντανές — τίμιοι χαρακτηρισμοί, χωρίς φουσκωμένες υποσχέσεις."""
    if not _gate(slug):
        return jsonify({"error": "unauthorized"}), 403
    def _has(*keys):
        return all(os.environ.get(k) for k in keys)
    sms = _has("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN") and \
        (os.environ.get("TWILIO_PHONE_NUMBER") or os.environ.get("TWILIO_DEFAULT_FROM"))
    return jsonify({"bridges": [
        {"name": "FHIR (read + ingest)", "state": "LIVE",
         "note": "Συνδέεται με ΟΠΟΙΟΔΗΠΟΤΕ σύστημα μιλάει FHIR (/fhir/<slug>/*)."},
        {"name": "Email (Resend)", "state": "LIVE" if _has("RESEND_API_KEY", "FROM_DOMAIN") else "needs_key",
         "note": "RESEND_API_KEY + FROM_DOMAIN"},
        {"name": "SMS (Twilio)", "state": "LIVE" if sms else "needs_key",
         "note": "TWILIO_ACCOUNT_SID + AUTH_TOKEN + PHONE_NUMBER"},
        {"name": "Φωνή (Vapi)", "state": "LIVE" if _has("VAPI_API_KEY") else "needs_key",
         "note": "VAPI_API_KEY + ελληνικό νούμερο"},
        {"name": "Google Calendar", "state": "LIVE" if _has("GOOGLE_OAUTH_REFRESH_TOKEN") else "needs_key",
         "note": "GOOGLE_OAUTH_* (get_google_refresh_token.py)"},
        {"name": "Πληρωμές (Stripe)", "state": "LIVE" if _has("STRIPE_SECRET_KEY") else "needs_key",
         "note": "STRIPE_SECRET_KEY (live)"},
        {"name": "WhatsApp/Instagram (Meta)", "state": "LIVE" if _has("META_WHATSAPP_TOKEN") else "needs_key",
         "note": "META_WHATSAPP_TOKEN + PHONE_ID"},
        {"name": "myDATA / ΑΑΔΕ", "state": "needs_key",
         "note": "AADE credentials του ιατρείου (mydata.aade.gr)"},
        {"name": "ΗΔΙΚΑ / e-prescription", "state": "no_public_api",
         "note": "Χωρίς δημόσιο API τρίτου· μόνο ο γιατρός με προσωπικούς κωδικούς. EHDS: 2027-2029."},
        {"name": "Doctoranytime / ιδιωτικές ασφαλιστικές", "state": "no_public_api",
         "note": "Χωρίς δημόσιο API στην Ελλάδα σήμερα."},
    ]})


def register(app):
    app.register_blueprint(bp)
    print("[FHIR] blueprint loaded — /fhir/metadata /fhir/<slug>/*")
