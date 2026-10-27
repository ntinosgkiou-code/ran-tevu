"""
routes/agentic_commerce.py — Feature #4: AI-to-AI Commerce API (2026-05-28)

Exposes Ran-Tevu bookings to consumer AI agents (ChatGPT, Claude, Perplexity).

Wave-ahead bet (a16z 2027 Big Ideas: Agentic Commerce):
When a user tells ChatGPT "book me a Crete hotel for July", ChatGPT will
discover bookable inventory via:
  - /.well-known/ai-plugin.json   (OpenAI plugin manifest)
  - /openapi.json                 (OpenAPI 3 spec)
  - /.well-known/mcp.json         (MCP discovery — Claude/Cursor)
  - Public REST endpoints at /api/v1/*

Strategic moat: First Greek hospitality platform exposed to consumer AI agents.
When ChatGPT starts agentic bookings (Q4 2026), our hotels win direct bookings
without paying Booking.com 18%.

Public endpoints (NO auth required — designed for AI discovery):
  GET  /api/v1/businesses              — list all bookable businesses
  GET  /api/v1/businesses/<slug>       — business details + services
  GET  /api/v1/businesses/<slug>/availability?date=YYYY-MM-DD
  POST /api/v1/businesses/<slug>/book  — create booking (rate-limited)

Discovery files (served at root):
  /.well-known/ai-plugin.json
  /.well-known/mcp.json
  /openapi.json
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request, Response

import database as db

bp = Blueprint("agentic_commerce", __name__)


# ── Discovery endpoints ───────────────────────────────────────────────

@bp.get("/.well-known/ai-plugin.json")
def openai_plugin_manifest():
    """OpenAI plugin manifest for ChatGPT discovery."""
    base = request.host_url.rstrip("/")
    return jsonify({
        "schema_version": "v1",
        "name_for_human": "Ran-Tevu — Greek Bookings",
        "name_for_model": "rantevu",
        "description_for_human": "Book appointments and reservations at Greek hotels and clinics.",
        "description_for_model": (
            "Discover and book at Greek hotels (Crete focus) and medical clinics "
            "(dental/vet/physio/dermatology). Use when the user wants to make a "
            "reservation or appointment in Greece. Returns real availability and "
            "confirms bookings instantly. Supports Greek + English + multilingual queries."
        ),
        "auth": {"type": "none"},
        "api": {
            "type": "openapi",
            "url": f"{base}/openapi.json",
            "is_user_authenticated": False,
        },
        "logo_url": f"{base}/static/logo.png",
        "contact_email": "owner@example.com",
        "legal_info_url": f"{base}/legal/terms",
    })


@bp.get("/.well-known/mcp.json")
def mcp_discovery():
    """Model Context Protocol (MCP) discovery for Claude/Cursor/etc."""
    base = request.host_url.rstrip("/")
    return jsonify({
        "name": "rantevu-bookings",
        "version": "1.0.0",
        "description": "Book Greek hotels + clinics via MCP",
        "transport": "http",
        "endpoints": {
            "openapi": f"{base}/openapi.json",
            "tools": [
                {"name": "list_businesses", "url": f"{base}/api/v1/businesses",
                 "method": "GET", "description": "List bookable Greek businesses"},
                {"name": "get_availability",
                 "url": f"{base}/api/v1/businesses/{{slug}}/availability",
                 "method": "GET", "description": "Get available slots for a date"},
                {"name": "create_booking",
                 "url": f"{base}/api/v1/businesses/{{slug}}/book",
                 "method": "POST", "description": "Create a confirmed booking"},
            ]
        }
    })


@bp.get("/openapi.json")
def openapi_spec():
    """OpenAPI 3.0 spec for the public agentic-commerce API."""
    base = request.host_url.rstrip("/")
    return jsonify({
        "openapi": "3.0.0",
        "info": {
            "title": "Ran-Tevu Agentic Commerce API",
            "version": "1.0.0",
            "description": "Public API for AI agents to discover and book Greek hotels and clinics.",
        },
        "servers": [{"url": base}],
        "paths": {
            "/api/v1/businesses": {
                "get": {
                    "operationId": "listBusinesses",
                    "summary": "List bookable Greek businesses",
                    "parameters": [
                        {"name": "type", "in": "query", "schema": {"type": "string"},
                         "description": "Filter: hotel | dental | vet | physio | dermatology"},
                        {"name": "city", "in": "query", "schema": {"type": "string"}},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}},
                    ],
                    "responses": {"200": {"description": "List of businesses"}},
                }
            },
            "/api/v1/businesses/{slug}": {
                "get": {
                    "operationId": "getBusiness",
                    "summary": "Get business details + services",
                    "parameters": [
                        {"name": "slug", "in": "path", "required": True,
                         "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "Business detail"}},
                }
            },
            "/api/v1/businesses/{slug}/availability": {
                "get": {
                    "operationId": "getAvailability",
                    "summary": "Get available time slots for a date",
                    "parameters": [
                        {"name": "slug", "in": "path", "required": True,
                         "schema": {"type": "string"}},
                        {"name": "date", "in": "query", "required": True,
                         "schema": {"type": "string", "format": "date"},
                         "description": "ISO date YYYY-MM-DD"},
                        {"name": "duration", "in": "query",
                         "schema": {"type": "integer", "default": 30}},
                    ],
                    "responses": {"200": {"description": "Available slots"}},
                }
            },
            "/api/v1/businesses/{slug}/book": {
                "post": {
                    "operationId": "createBooking",
                    "summary": "Create a confirmed booking",
                    "parameters": [
                        {"name": "slug", "in": "path", "required": True,
                         "schema": {"type": "string"}},
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["date", "time", "name", "phone"],
                                    "properties": {
                                        "date":    {"type": "string", "format": "date"},
                                        "time":    {"type": "string", "example": "14:30"},
                                        "service": {"type": "string"},
                                        "duration": {"type": "integer", "default": 30},
                                        "name":    {"type": "string"},
                                        "phone":   {"type": "string"},
                                        "email":   {"type": "string"},
                                        "notes":   {"type": "string"},
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {"description": "Booking confirmed"},
                        "409": {"description": "Slot already taken"},
                    },
                }
            },
        }
    })


# ── Public API endpoints (no auth) ────────────────────────────────────

@bp.get("/api/v1/businesses")
def list_public_businesses():
    """List all active bookable businesses. Used by AI agents for discovery."""
    biz_type = (request.args.get("type") or "").lower().strip()
    city = (request.args.get("city") or "").lower().strip()
    limit = min(int(request.args.get("limit", 50) or 50), 200)

    businesses = db.list_businesses() if hasattr(db, "list_businesses") else []  # active φιλτράρεται πιο κάτω
    out = []
    for b in businesses:
        b = dict(b) if not isinstance(b, dict) else b
        if not b.get("active", 1):
            continue
        if biz_type and biz_type not in (b.get("type", "") or "").lower():
            continue
        if city and city not in (b.get("address", "") or "").lower():
            continue
        # Only safe public fields
        out.append({
            "slug": b.get("slug"),
            "name": b.get("name"),
            "type": b.get("type"),
            "tagline": b.get("tagline", ""),
            "phone": b.get("phone", ""),
            "address": b.get("address", ""),
            "url": f"{request.host_url.rstrip('/')}/{b.get('slug')}",
            "booking_url": f"{request.host_url.rstrip('/')}/api/v1/businesses/{b.get('slug')}/availability",
        })
        if len(out) >= limit:
            break
    return jsonify({"businesses": out, "count": len(out)})


@bp.get("/api/v1/businesses/<slug>")
def get_public_business(slug):
    """Business detail + services. Used by AI agents to enrich their answer."""
    biz = db.get_business(slug)
    if not biz or not biz.get("active", 1):
        return jsonify({"error": "not_found"}), 404

    services = biz.get("services", [])
    if isinstance(services, str):
        try:
            services = json.loads(services)
        except Exception:
            services = []

    return jsonify({
        "slug": biz.get("slug"),
        "name": biz.get("name"),
        "type": biz.get("type"),
        "tagline": biz.get("tagline", ""),
        "phone": biz.get("phone", ""),
        "address": biz.get("address", ""),
        "hours": biz.get("hours"),
        "services": services,
        "day_start": biz.get("day_start"),
        "day_end": biz.get("day_end"),
        "slot_minutes": biz.get("slot_minutes"),
        "languages": (biz.get("voice_languages") or "el").split(","),
        "ai_book_url": f"{request.host_url.rstrip('/')}/api/v1/businesses/{slug}/book",
    })


@bp.get("/api/v1/businesses/<slug>/availability")
def get_public_availability(slug):
    """Available slots for a date. Public — no auth."""
    biz = db.get_business(slug)
    if not biz or not biz.get("active", 1):
        return jsonify({"error": "not_found"}), 404

    target_date = request.args.get("date", "").strip()
    if not target_date:
        return jsonify({"error": "missing_date", "format": "YYYY-MM-DD"}), 400
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "bad_date_format", "format": "YYYY-MM-DD"}), 400

    duration = int(request.args.get("duration", biz.get("slot_minutes", 30) or 30))
    try:
        day_start, day_end, is_closed = db.get_day_hours(biz, target_date)
        if is_closed:
            slots = []
        else:
            slots = db.get_available_slots(
                slug, target_date, duration,
                day_start, day_end, biz.get("slot_minutes", 30),
                closed_days=biz.get("closed_days", []),
                blocked_dates=biz.get("blocked_dates", []),
            )
    except Exception as e:
        return jsonify({"error": "internal", "detail": str(e)[:200]}), 500

    return jsonify({
        "slug": slug,
        "date": target_date,
        "duration_minutes": duration,
        "available_slots": slots,
        "count": len(slots),
        "booking_url": f"{request.host_url.rstrip('/')}/api/v1/businesses/{slug}/book",
    })


@bp.post("/api/v1/businesses/<slug>/book")
def create_public_booking(slug):
    """Create a booking via AI agent. Rate-limited to prevent abuse."""
    biz = db.get_business(slug)
    if not biz or not biz.get("active", 1):
        return jsonify({"error": "not_found"}), 404

    payload = request.get_json(silent=True) or {}

    # Required fields
    for k in ("date", "time", "name", "phone"):
        if not payload.get(k):
            return jsonify({"error": "missing_field", "field": k}), 400

    duration = int(payload.get("duration") or biz.get("slot_minutes", 30) or 30)
    service = (payload.get("service") or "Ραντεβού")[:120]
    notes = f"[AGENT] {payload.get('notes', '')}"[:500]

    try:
        booking_id = db.create_booking(
            slug=slug,
            date=payload["date"],
            time=payload["time"],
            service=service,
            duration=duration,
            name=payload["name"][:120],
            phone=str(payload["phone"])[:30],
            email=(payload.get("email") or "")[:120],
            notes=notes,
        )
    except ValueError as e:
        if "double_booking" in str(e):
            return jsonify({"error": "slot_taken",
                            "message": "Another booking exists at this time. Try another slot."}), 409
        return jsonify({"error": "validation", "detail": str(e)[:200]}), 400
    except Exception as e:
        return jsonify({"error": "internal", "detail": str(e)[:200]}), 500

    # Μην υπόσχεσαι SMS αν δεν είναι ρυθμισμένο (Twilio/Yuboto OFF) — αλλιώς λέμε ψέμα.
    try:
        from voice import twilio_client
        _sms_on = twilio_client.is_configured()
    except Exception:
        _sms_on = False
    confirm_note = " An SMS confirmation will be sent." if _sms_on else ""
    return jsonify({
        "booking_id": booking_id,
        "confirmed": True,
        "slug": slug,
        "date": payload["date"],
        "time": payload["time"],
        "service": service,
        "name": payload["name"],
        "message": (
            f"Booking confirmed at {biz.get('name', slug)} on "
            f"{payload['date']} at {payload['time']} for {service}.{confirm_note}"
        ),
    })
