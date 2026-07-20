import os
import json
import sqlite3
from datetime import datetime, date, timedelta
from typing import Optional

DB = os.environ.get("DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookings.db")


def _check_persistence():
    """⚠️ Railway filesystem ΕΙΝΑΙ ephemeral: αν το DB δεν είναι σε mounted volume,
    ΚΑΘΕ deploy/restart σβήνει ΟΛΑ τα δεδομένα πελατών. Προειδοποιεί δυνατά —
    fix: Railway → Volume mount (π.χ. /data) + DB_PATH=/data/bookings.db."""
    on_railway = bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_ENVIRONMENT"))
    persistent = any(DB.startswith(p) for p in ("/data", "/mnt", "/var/lib")) or "RAILWAY_VOLUME_MOUNT_PATH" in os.environ
    if on_railway and not persistent:
        print("=" * 70)
        print("[DATA LOSS RISK ⚠️] Το SQLite ΔΕΝ είναι σε persistent volume στο Railway.")
        print(f"  DB path: {DB}")
        print("  ΚΑΘΕ deploy/restart θα σβήσει όλα τα bookings/businesses.")
        print("  FIX: Railway → Add Volume (mount /data) + env DB_PATH=/data/bookings.db")
        print("=" * 70)


_check_persistence()


def _conn():
    c = sqlite3.connect(DB, timeout=10)   # timeout=10 = busy_timeout 10s (locked-DB guard)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as conn:
        # ── Businesses ────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS businesses (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                slug         TEXT UNIQUE NOT NULL,
                name         TEXT NOT NULL,
                type         TEXT DEFAULT 'επιχείρηση',
                tagline      TEXT DEFAULT '',
                phone        TEXT DEFAULT '',
                address      TEXT DEFAULT '',
                owner_email  TEXT DEFAULT '',
                owner_name   TEXT DEFAULT '',
                hours        TEXT NOT NULL DEFAULT '{}',
                services     TEXT NOT NULL DEFAULT '[]',
                upsells      TEXT DEFAULT '[]',
                slot_minutes INTEGER DEFAULT 30,
                day_start    TEXT DEFAULT '10:00',
                day_end      TEXT DEFAULT '20:00',
                smtp_email   TEXT DEFAULT '',
                smtp_password TEXT DEFAULT '',
                admin_token  TEXT DEFAULT '',
                active       INTEGER DEFAULT 1,
                created      TEXT DEFAULT (datetime('now'))
            )
        """)

        # ── Bookings ──────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                slug        TEXT NOT NULL DEFAULT 'default',
                date        TEXT NOT NULL,
                time        TEXT NOT NULL,
                service     TEXT NOT NULL,
                duration    INTEGER NOT NULL,
                name        TEXT NOT NULL,
                phone       TEXT NOT NULL,
                email       TEXT,
                notes       TEXT,
                status      TEXT DEFAULT 'confirmed',
                reminder_24 INTEGER DEFAULT 0,
                reminder_4  INTEGER DEFAULT 0,
                created     TEXT DEFAULT (datetime('now'))
            )
        """)

        # ── Client profiles ───────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS client_profiles (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                slug         TEXT NOT NULL DEFAULT 'default',
                phone        TEXT NOT NULL,
                name         TEXT,
                email        TEXT,
                preferences  TEXT DEFAULT '',
                skin_notes   TEXT DEFAULT '',
                visit_count  INTEGER DEFAULT 0,
                last_visit   TEXT,
                first_visit  TEXT,
                last_outreach TEXT DEFAULT NULL,
                updated      TEXT DEFAULT (datetime('now')),
                UNIQUE(slug, phone)
            )
        """)

        # ── Waitlist ──────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS waitlist (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                slug       TEXT NOT NULL DEFAULT 'default',
                date       TEXT NOT NULL,
                time       TEXT NOT NULL,
                service    TEXT NOT NULL,
                name       TEXT NOT NULL,
                phone      TEXT NOT NULL,
                email      TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # ── Pending waitlist claims (one-click "πάρε τη θέση") ───────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_claims (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                slug        TEXT NOT NULL,
                date        TEXT NOT NULL,
                time        TEXT NOT NULL,
                service     TEXT NOT NULL,
                name        TEXT NOT NULL,
                phone       TEXT NOT NULL,
                email       TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                expires_at  TEXT NOT NULL,
                claimed     INTEGER DEFAULT 0
            )
        """)

        # ── Email outreach tracking (cold email campaign) ────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_unsubscribes (
                email       TEXT PRIMARY KEY,
                created_at  TEXT DEFAULT (datetime('now')),
                source      TEXT DEFAULT 'link'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_sent_log (
                email       TEXT PRIMARY KEY,
                campaign    TEXT DEFAULT 'cold',
                sent_at     TEXT DEFAULT (datetime('now')),
                status      TEXT DEFAULT 'ok'
            )
        """)

        # ════════════════════════════════════════════════════════════════════
        # AutoSales — outreach pipeline (Phase 1+2+3 scaffolding)
        # ════════════════════════════════════════════════════════════════════
        conn.execute("PRAGMA journal_mode=WAL")

        # ─── Leads ──────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS outreach_leads (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                source              TEXT NOT NULL,
                business_name       TEXT NOT NULL,
                industry            TEXT,
                city                TEXT,
                address             TEXT,
                phone               TEXT,
                email               TEXT,
                website             TEXT,
                instagram           TEXT,
                facebook            TEXT,
                linkedin            TEXT,
                whatsapp            TEXT,
                viber               TEXT,
                notes               TEXT,
                score               INTEGER DEFAULT 50,
                status              TEXT DEFAULT 'new',
                last_outreach_at    TEXT,
                enriched_at         TEXT,
                created_at          TEXT DEFAULT (datetime('now')),
                updated_at          TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_leads_email ON outreach_leads(email) WHERE email IS NOT NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outreach_leads_status   ON outreach_leads(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outreach_leads_industry ON outreach_leads(industry)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outreach_leads_city     ON outreach_leads(city)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outreach_leads_score    ON outreach_leads(score DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outreach_leads_created  ON outreach_leads(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outreach_leads_outreach ON outreach_leads(last_outreach_at)")

        # ─── Messages (outbound) ────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS outreach_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id         INTEGER NOT NULL REFERENCES outreach_leads(id),
                channel         TEXT NOT NULL,
                sequence_step   INTEGER NOT NULL DEFAULT 0,
                direction       TEXT NOT NULL DEFAULT 'out',
                subject         TEXT,
                body            TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                failure_reason  TEXT,
                provider_id     TEXT,
                scheduled_at    TEXT,
                sent_at         TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX        IF NOT EXISTS idx_outreach_messages_lead           ON outreach_messages(lead_id)")
        conn.execute("CREATE INDEX        IF NOT EXISTS idx_outreach_messages_channel_status ON outreach_messages(channel, status)")
        conn.execute("CREATE INDEX        IF NOT EXISTS idx_outreach_messages_due            ON outreach_messages(scheduled_at) WHERE status='pending'")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_messages_dedup          ON outreach_messages(lead_id, channel, sequence_step)")

        # ─── Replies (inbound) ──────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS outreach_replies (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id         INTEGER REFERENCES outreach_leads(id),
                message_id      INTEGER REFERENCES outreach_messages(id),
                channel         TEXT NOT NULL,
                raw_text        TEXT NOT NULL,
                classification  TEXT,
                sentiment       TEXT,
                handled         INTEGER NOT NULL DEFAULT 0,
                handled_by      TEXT,
                received_at     TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outreach_replies_handled        ON outreach_replies(handled, received_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outreach_replies_classification ON outreach_replies(classification)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outreach_replies_lead           ON outreach_replies(lead_id)")

        # ─── Blacklist (GDPR + bounces) ─────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS outreach_blacklist (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier      TEXT NOT NULL,
                channel         TEXT NOT NULL,
                reason          TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_blacklist_id_channel ON outreach_blacklist(identifier, channel)")

        # ─── Channel state (rate limiting + auto-pause) ─────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS outreach_channel_state (
                channel         TEXT PRIMARY KEY,
                enabled         INTEGER NOT NULL DEFAULT 1,
                paused_until    TEXT,
                pause_reason    TEXT,
                sent_today      INTEGER NOT NULL DEFAULT 0,
                last_send_at    TEXT,
                daily_reset_at  TEXT,
                daily_limit     INTEGER NOT NULL DEFAULT 50,
                updated_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        for _ch, _lim in [
            ("email", 100), ("instagram", 30), ("facebook", 20), ("whatsapp", 100),
            ("viber", 50), ("sms", 100), ("linkedin", 25), ("voice", 10),
        ]:
            conn.execute(
                "INSERT OR IGNORE INTO outreach_channel_state (channel, daily_limit) VALUES (?, ?)",
                (_ch, _lim),
            )

        # ─── Growth Engine: Content posts (LinkedIn/IG/FB) ──────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS content_posts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                platform      TEXT NOT NULL,           -- linkedin|instagram|facebook
                language      TEXT NOT NULL DEFAULT 'gr',  -- gr|en
                post_type     TEXT DEFAULT 'text',     -- text|image|video|carousel
                title         TEXT DEFAULT '',
                body          TEXT NOT NULL,
                hashtags      TEXT DEFAULT '',
                cta_url       TEXT DEFAULT '',
                media_url     TEXT DEFAULT '',         -- YouTube/Vimeo/image
                target_region TEXT DEFAULT 'global',   -- gr|eu|us|global
                status        TEXT NOT NULL DEFAULT 'pending',
                                                       -- pending|approved|rejected|published|failed
                buffer_id     TEXT DEFAULT '',         -- id από Buffer όταν δημοσιευτεί
                published_url TEXT DEFAULT '',         -- final post URL
                impressions   INTEGER DEFAULT 0,
                clicks        INTEGER DEFAULT 0,
                conversions   INTEGER DEFAULT 0,       -- demo signups / Ran-Tevu trials
                generated_at  TEXT DEFAULT (datetime('now')),
                approved_at   TEXT,
                published_at  TEXT,
                approved_by   TEXT DEFAULT '',
                notes         TEXT DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_status ON content_posts(status, platform)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_published ON content_posts(published_at DESC)")

        # ─── Growth Engine: Funnel events (post → click → demo → signup) ───
        conn.execute("""
            CREATE TABLE IF NOT EXISTS funnel_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type   TEXT NOT NULL,    -- impression|click|demo_view|signup|trial_start|paid
                source       TEXT NOT NULL,    -- linkedin|instagram|facebook|email|direct
                campaign     TEXT DEFAULT '',  -- post_id ή email_id
                lead_email   TEXT DEFAULT '',
                lead_country TEXT DEFAULT '',
                metadata     TEXT DEFAULT '{}', -- JSON: utm_*, post_id, ip, etc
                created_at   TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_funnel_type ON funnel_events(event_type, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_funnel_source ON funnel_events(source, created_at DESC)")

        # ─── Growth Engine: Audience metrics (followers, reach per platform) ─
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audience_metrics (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                platform    TEXT NOT NULL,
                metric_date TEXT NOT NULL,
                followers   INTEGER DEFAULT 0,
                reach       INTEGER DEFAULT 0,
                engagement  INTEGER DEFAULT 0,
                profile_visits INTEGER DEFAULT 0,
                website_clicks INTEGER DEFAULT 0,
                UNIQUE(platform, metric_date)
            )
        """)

        # ─── Storefront: Orders ──────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS store_orders (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                product_slug          TEXT NOT NULL,
                customer_email        TEXT NOT NULL,
                customer_name         TEXT DEFAULT '',
                customer_country      TEXT DEFAULT '',
                customer_language     TEXT DEFAULT 'gr',
                amount_cents          INTEGER NOT NULL,
                currency              TEXT DEFAULT 'eur',
                stripe_session_id     TEXT UNIQUE,
                stripe_payment_intent TEXT,
                status                TEXT NOT NULL DEFAULT 'pending',
                                                       -- pending|paid|delivering|delivered|failed|refunded
                custom_input          TEXT DEFAULT '{}',  -- JSON με ό,τι έδωσε ο πελάτης
                delivery_attempts     INTEGER DEFAULT 0,
                last_error            TEXT DEFAULT '',
                created_at            TEXT DEFAULT (datetime('now')),
                paid_at               TEXT,
                delivered_at          TEXT,
                referrer              TEXT DEFAULT ''   -- από πού ήρθε (utm)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON store_orders(status, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_email ON store_orders(customer_email)")

        # ─── Storefront: Deliverables (generated content per order) ──────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS store_deliveries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id     INTEGER NOT NULL,
                content      TEXT NOT NULL,
                content_type TEXT DEFAULT 'markdown',
                tokens_used  INTEGER DEFAULT 0,
                model_used   TEXT DEFAULT '',
                generated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (order_id) REFERENCES store_orders(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deliv_order ON store_deliveries(order_id)")

        # ── Founding members tracking ────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS founding_members (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                business_name       TEXT NOT NULL,
                business_type       TEXT,
                owner_name          TEXT,
                email               TEXT NOT NULL,
                phone               TEXT,
                address             TEXT,
                services            TEXT,
                hours               TEXT,
                emergency_handling  TEXT,
                special_notes       TEXT,
                status              TEXT DEFAULT 'pending',
                created_at          TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_founding_email ON founding_members(email)")

        # ── Voice agents (one per business) ──────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS voice_agents (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                business_slug         TEXT UNIQUE NOT NULL,
                vapi_assistant_id     TEXT,
                vapi_phone_id         TEXT,
                twilio_phone_sid      TEXT,
                twilio_phone_number   TEXT,
                voice_key             TEXT DEFAULT 'madlen',
                assistant_name        TEXT DEFAULT 'Madlen',
                status                TEXT DEFAULT 'active',
                last_provisioned      TEXT,
                config_json           TEXT,
                created_at            TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_agent_slug ON voice_agents(business_slug)")

        # ── Voice calls (call log + transcripts) ─────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS voice_calls (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                business_slug            TEXT NOT NULL,
                vapi_call_id             TEXT,
                twilio_call_sid          TEXT,
                from_number              TEXT,
                to_number                TEXT,
                started_at               TEXT,
                ended_at                 TEXT,
                duration_seconds         INTEGER DEFAULT 0,
                cost_usd                 REAL DEFAULT 0,
                transcript               TEXT,
                summary                  TEXT,
                structured_data_json     TEXT,
                success_evaluation       TEXT,
                recording_url            TEXT,
                ended_reason             TEXT,
                created_at               TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_calls_slug ON voice_calls(business_slug, started_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_calls_vapi ON voice_calls(vapi_call_id)")
        # P5 idempotency: διπλά Vapi end-of-call webhooks δεν πρέπει να γράφουν
        # διπλές εγγραφές. Dedupe τυχόν υπάρχοντα διπλά, μετά unique partial index.
        try:
            conn.execute("""
                DELETE FROM voice_calls WHERE id NOT IN (
                    SELECT MIN(id) FROM voice_calls
                    WHERE vapi_call_id IS NOT NULL GROUP BY vapi_call_id
                ) AND vapi_call_id IS NOT NULL
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_calls_vapi
                ON voice_calls(vapi_call_id) WHERE vapi_call_id IS NOT NULL
            """)
        except Exception as _e:
            print(f"[DB] voice_calls unique index migration skipped: {_e}")

        # ── Voice call events (audit log για debugging) ──────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS voice_call_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                business_slug   TEXT NOT NULL,
                vapi_call_id    TEXT,
                event_type      TEXT,
                payload_json    TEXT,
                response_json   TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_events_call ON voice_call_events(vapi_call_id, created_at)")

        # ── Customer Memory (AI θυμάται κάθε πελάτη — wave: AI memory layer) ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customer_memory (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                slug              TEXT NOT NULL,
                identifier        TEXT NOT NULL,
                identifier_type   TEXT DEFAULT 'phone',
                display_name      TEXT DEFAULT '',
                personal_notes    TEXT DEFAULT '',
                family_info       TEXT DEFAULT '',
                preferred_time    TEXT DEFAULT '',
                preferred_staff   TEXT DEFAULT '',
                preferred_service TEXT DEFAULT '',
                last_topics       TEXT DEFAULT '',
                conversation_summary TEXT DEFAULT '',
                interaction_count INTEGER DEFAULT 0,
                last_seen         TEXT,
                created           TEXT DEFAULT (datetime('now')),
                updated           TEXT DEFAULT (datetime('now')),
                UNIQUE(slug, identifier)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_slug_id ON customer_memory(slug, identifier)")

        # ── Audit Logs (EU AI Act compliance) ─────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                slug        TEXT,
                action      TEXT NOT NULL,
                actor_type  TEXT DEFAULT 'system',
                actor_id    TEXT,
                model       TEXT,
                customer_id TEXT,
                ip_hash     TEXT,
                details     TEXT DEFAULT '',
                created     TEXT DEFAULT (datetime('now')),
                prev_hash   TEXT,
                row_hash    TEXT
            )
        """)
        # migration for existing DBs (CREATE IF NOT EXISTS won't add columns)
        for _col in ("prev_hash TEXT", "row_hash TEXT"):
            try:
                conn.execute(f"ALTER TABLE audit_logs ADD COLUMN {_col}")
            except Exception:
                pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_slug ON audit_logs(slug, created DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_customer ON audit_logs(customer_id, created DESC)")

        # ── Consent Logs (GDPR + EU AI Act tracking) ──────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS consent_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                slug            TEXT,
                identifier      TEXT,
                identifier_type TEXT DEFAULT 'session',
                consent_type    TEXT NOT NULL,
                consent_version TEXT DEFAULT '1.0',
                accepted        INTEGER DEFAULT 1,
                ip_hash         TEXT,
                user_agent      TEXT DEFAULT '',
                created         TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_consent_identifier ON consent_logs(identifier, created DESC)")

        # ── Unified Inbox (Feature #2: Multi-channel) ───────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inbox_threads (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                slug            TEXT NOT NULL,
                channel         TEXT NOT NULL,  -- whatsapp | instagram | booking | airbnb | sms | email | web
                external_id     TEXT,           -- provider's thread/conversation id
                guest_identifier TEXT,          -- phone or username or email
                guest_name      TEXT DEFAULT '',
                guest_language  TEXT DEFAULT 'el',
                status          TEXT DEFAULT 'open',  -- open | resolved | escalated
                last_message_at TEXT,
                created         TEXT DEFAULT (datetime('now')),
                UNIQUE(slug, channel, external_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_slug_open ON inbox_threads(slug, status, last_message_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_guest ON inbox_threads(slug, guest_identifier)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS inbox_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id       INTEGER NOT NULL,
                direction       TEXT NOT NULL,  -- inbound | outbound
                sender          TEXT NOT NULL,  -- 'guest' | 'ai' | 'owner'
                body            TEXT NOT NULL,
                media_url       TEXT,
                external_msg_id TEXT,
                ai_handled      INTEGER DEFAULT 0,
                created         TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (thread_id) REFERENCES inbox_threads(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_msg_thread ON inbox_messages(thread_id, created DESC)")

        # ── Outbound Jobs (Feature #3: Outbound Proactive AI) ────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS outbound_jobs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                slug         TEXT NOT NULL,
                job_type     TEXT NOT NULL,  -- pre_arrival_upsell | no_show_reminder | review_request | win_back
                channel      TEXT NOT NULL DEFAULT 'voice',  -- voice | sms | email
                customer_phone TEXT NOT NULL,
                customer_name TEXT DEFAULT '',
                customer_email TEXT DEFAULT '',
                booking_id   INTEGER,
                payload_json TEXT DEFAULT '{}',
                scheduled_at TEXT NOT NULL,
                status       TEXT DEFAULT 'pending',  -- pending | done | failed | cancelled
                result       TEXT DEFAULT '',
                attempts     INTEGER DEFAULT 0,
                created      TEXT DEFAULT (datetime('now')),
                processed_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbound_due ON outbound_jobs(status, scheduled_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbound_slug ON outbound_jobs(slug, scheduled_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbound_booking ON outbound_jobs(booking_id)")

        # ── Migrate existing tables (παλιά βάση χωρίς slug) ──────────────────
        for col_def in [
            ("bookings",        "slug TEXT NOT NULL DEFAULT 'default'"),
            ("bookings",        "reminder_24 INTEGER DEFAULT 0"),
            ("bookings",        "reminder_4 INTEGER DEFAULT 0"),
            ("bookings",        "gcal_event_id TEXT"),
            ("client_profiles", "slug TEXT NOT NULL DEFAULT 'default'"),
            ("client_profiles", "last_outreach TEXT DEFAULT NULL"),
            ("waitlist",        "slug TEXT NOT NULL DEFAULT 'default'"),
            ("businesses",      "en_service_names TEXT DEFAULT '{}'"),
            ("businesses",      "accent_color TEXT DEFAULT '#7C3AED'"),
            ("businesses",      "google_review_url TEXT DEFAULT ''"),
            ("businesses",      "google_calendar_id TEXT DEFAULT ''"),
            ("businesses",      "questionnaire TEXT DEFAULT ''"),
            ("bookings",        "review_sent INTEGER DEFAULT 0"),
            ("bookings",        "questionnaire_sent INTEGER DEFAULT 0"),
            ("businesses",      "closed_days TEXT DEFAULT '[]'"),
            ("businesses",      "blocked_dates TEXT DEFAULT '[]'"),
            # Voice customization fields (per business)
            ("businesses",      "voice_key TEXT DEFAULT 'madlen'"),
            ("businesses",      "voice_assistant_name TEXT DEFAULT 'Madlen'"),
            ("businesses",      "voice_tone TEXT DEFAULT 'friendly'"),
            ("businesses",      "voice_custom_instructions TEXT DEFAULT ''"),
            ("businesses",      "voice_greeting TEXT DEFAULT ''"),
            ("businesses",      "voice_languages TEXT DEFAULT 'el'"),
            # Daily briefing (ο AI ενημερώνει τον γιατρό μόνος του)
            ("businesses",      "briefing_enabled INTEGER DEFAULT 1"),
            ("businesses",      "briefing_channel TEXT DEFAULT 'auto'"),
            # Risk-based escalated reminder (risk_brain: high-risk → 1 έξτρα υπενθύμιση ~48h πριν)
            ("bookings",        "reminder_risk INTEGER DEFAULT 0"),
            # Unified Inbox: per-business Meta channel ids, ώστε πολλά ιατρεία να μοιράζονται
            # τα ίδια webhooks/webhooks/instagram χωρίς να πέφτουν όλα σε ένα hardcoded slug.
            ("businesses",      "whatsapp_phone_id TEXT DEFAULT ''"),
            ("businesses",      "instagram_page_id TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE {col_def[0]} ADD COLUMN {col_def[1]}")
            except sqlite3.OperationalError:
                pass

        # ── Double-booking backstop: DB-level atomic guard ───────────────────
        # Partial unique index (ignores cancelled) — the second concurrent INSERT
        # for the same slot fails with IntegrityError, which create_booking maps
        # to ValueError('double_booking'). Legacy duplicate rows (from before this
        # guard existed) would otherwise block the CREATE UNIQUE INDEX below forever
        # — resolve them first: keep the EARLIEST non-cancelled booking per exact
        # (slug,date,time), mark any other non-cancelled booking on that same slot as
        # 'cancelled' (never deleted — the row + its history survive the audit trail,
        # it's just taken out of the active slot, exactly what would have to happen
        # anyway since only one patient can occupy one slot). One-time + idempotent:
        # a slot with 0 or 1 active bookings is a no-op on every subsequent boot.
        _dupe_groups = conn.execute("""
            SELECT slug, date, time, COUNT(*) as n FROM bookings
            WHERE status != 'cancelled'
            GROUP BY slug, date, time HAVING COUNT(*) > 1
        """).fetchall()
        if _dupe_groups:
            _resolved = 0
            for _g in _dupe_groups:
                _rows = conn.execute(
                    "SELECT id FROM bookings WHERE slug=? AND date=? AND time=? "
                    "AND status != 'cancelled' ORDER BY id ASC",
                    (_g["slug"], _g["date"], _g["time"])
                ).fetchall()
                for _extra in _rows[1:]:          # keep _rows[0] (earliest), cancel the rest
                    conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (_extra["id"],))
                    _resolved += 1
            conn.commit()
            print(f"[init_db] dedup migration: resolved {len(_dupe_groups)} duplicate slot(s), "
                  f"{_resolved} legacy booking(s) marked cancelled (earliest kept per slot)")

        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_slot "
                "ON bookings(slug, date, time) WHERE status != 'cancelled'"
            )
        except Exception as _ie:
            print(f"[init_db] slot unique index skipped (existing duplicates?): {_ie}")

        # ── Recovered-Revenue Ledger (proof-of-value: € won back, honest, append-only) ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recovery_ledger (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                slug          TEXT NOT NULL,
                booking_id    INTEGER,
                patient_name  TEXT DEFAULT '',
                patient_phone TEXT DEFAULT '',
                recovery_type TEXT DEFAULT 'no_show_recovery',
                amount_eur    REAL DEFAULT 0,
                basis         TEXT DEFAULT '',
                source        TEXT DEFAULT '',
                created       TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recovery_slug ON recovery_ledger(slug, created DESC)")
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_booking ON recovery_ledger(booking_id) WHERE booking_id IS NOT NULL")
        except Exception:
            pass

        conn.commit()


# ════════════════════════════════════════════════════════════════════
#  BUSINESS MANAGEMENT
# ════════════════════════════════════════════════════════════════════

def insert_founding_member(payload: dict) -> int:
    """Saves a founding-member onboarding submission. Returns row id."""
    with _conn() as c:
        cur = c.execute("""
            INSERT INTO founding_members
              (business_name, business_type, owner_name, email, phone, address,
               services, hours, emergency_handling, special_notes)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            payload.get("business_name", "")[:200],
            payload.get("business_type", "")[:80],
            payload.get("owner_name", "")[:120],
            payload.get("email", "")[:200],
            payload.get("phone", "")[:40],
            payload.get("address", "")[:300],
            payload.get("services", "")[:3000],
            payload.get("hours", "")[:1000],
            payload.get("emergency_handling", "")[:300],
            payload.get("special_notes", "")[:2000],
        ))
        return cur.lastrowid


def count_founding_members(exclude_emails: tuple = ()) -> int:
    """Returns number of distinct founding members, excluding test/owner emails."""
    with _conn() as c:
        if exclude_emails:
            placeholders = ",".join(["?"] * len(exclude_emails))
            row = c.execute(
                f"SELECT COUNT(DISTINCT email) AS n FROM founding_members WHERE email NOT IN ({placeholders})",
                exclude_emails,
            ).fetchone()
        else:
            row = c.execute("SELECT COUNT(DISTINCT email) AS n FROM founding_members").fetchone()
        return int(row["n"]) if row else 0


def create_business(slug: str, config: dict) -> int:
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO businesses
              (slug, name, type, tagline, phone, address, owner_email, owner_name,
               hours, services, upsells, slot_minutes, day_start, day_end,
               smtp_email, smtp_password, admin_token, en_service_names, active)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
        """, (
            slug,
            config.get("name", ""),
            config.get("type", "επιχείρηση"),
            config.get("tagline", ""),
            config.get("phone", ""),
            config.get("address", ""),
            config.get("owner_email", ""),
            config.get("owner_name", ""),
            json.dumps(config.get("hours", {}), ensure_ascii=False),
            json.dumps(config.get("services", []), ensure_ascii=False),
            json.dumps(config.get("upsells", []), ensure_ascii=False),
            config.get("slot_minutes", 30),
            config.get("day_start", "10:00"),
            config.get("day_end", "20:00"),
            config.get("smtp_email", ""),
            config.get("smtp_password", ""),
            config.get("admin_token", ""),
            json.dumps(config.get("en_service_names", {}), ensure_ascii=False),
        ))
        conn.commit()
        return cur.lastrowid


def get_business(slug: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM businesses WHERE slug=? AND active=1", (slug,)
        ).fetchone()
    if not row:
        return None
    b = dict(row)
    b["hours"]            = json.loads(b["hours"]            or "{}")
    b["services"]         = json.loads(b["services"]         or "[]")
    b["upsells"]          = json.loads(b["upsells"]          or "[]")
    b["en_service_names"] = json.loads(b.get("en_service_names") or "{}")
    b["closed_days"]      = json.loads(b.get("closed_days")  or "[]")
    b["blocked_dates"]    = json.loads(b.get("blocked_dates") or "[]")
    return b


def update_business(slug: str, config: dict):
    fields, vals = [], []
    updatable = ["name","type","tagline","phone","address","owner_email","owner_name",
                 "slot_minutes","day_start","day_end","smtp_email","smtp_password","admin_token","active",
                 "accent_color","google_review_url","google_calendar_id","questionnaire",
                 "briefing_enabled","briefing_channel",
                 "voice_tone","voice_custom_instructions","voice_greeting",
                 "voice_assistant_name","voice_key"]
    for f in updatable:
        if f in config:
            fields.append(f"{f}=?")
            vals.append(config[f])
    for jf in ["hours","services","upsells","en_service_names","closed_days","blocked_dates"]:
        if jf in config:
            fields.append(f"{jf}=?")
            vals.append(json.dumps(config[jf], ensure_ascii=False))
    if not fields:
        return
    vals.append(slug)
    with _conn() as conn:
        conn.execute(f"UPDATE businesses SET {', '.join(fields)} WHERE slug=?", vals)
        conn.commit()


def rename_slug(old_slug: str, new_slug: str) -> bool:
    """Rename a business slug across all tables. Returns True if successful."""
    with _conn() as conn:
        # Check old exists and new doesn't
        existing = conn.execute("SELECT id FROM businesses WHERE slug=?", (old_slug,)).fetchone()
        if not existing:
            return False
        conflict = conn.execute("SELECT id FROM businesses WHERE slug=?", (new_slug,)).fetchone()
        if conflict:
            return False
        conn.execute("UPDATE businesses      SET slug=? WHERE slug=?", (new_slug, old_slug))
        conn.execute("UPDATE bookings        SET slug=? WHERE slug=?", (new_slug, old_slug))
        conn.execute("UPDATE client_profiles SET slug=? WHERE slug=?", (new_slug, old_slug))
        conn.execute("UPDATE waitlist        SET slug=? WHERE slug=?", (new_slug, old_slug))
        conn.commit()
    return True


def list_businesses() -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT slug, name, type, tagline, phone, address, active, created, admin_token FROM businesses ORDER BY created DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════
#  BOOKINGS
# ════════════════════════════════════════════════════════════════════

def get_bookings_for_date(slug: str, target_date: str) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE slug=? AND date=? AND status!='cancelled' ORDER BY time",
            (slug, target_date)
        ).fetchall()
    return [dict(r) for r in rows]


def get_day_hours(business: dict, target_date: str):
    """(day_start, day_end, is_closed) για την ημέρα του target_date, διαβάζοντας τα
    per-day business['hours'] με fallback στο day_start/day_end. Κοινό helper για
    voice + agentic availability. (Το chat έχει δικό του _get_day_hours στο app.py —
    TODO: ενοποίηση όταν είναι ασφαλές.)"""
    import unicodedata, re
    from datetime import datetime as _dt
    def _nrm(s):
        nfd = unicodedata.normalize('NFD', str(s))
        return ''.join(c for c in nfd if unicodedata.category(c) != 'Mn').lower().strip()
    _DAY_GR = ["Δευτέρα","Τρίτη","Τετάρτη","Πέμπτη","Παρασκευή","Σάββατο","Κυριακή"]
    _DAY_EN = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    ds = business.get("day_start", "10:00")
    de = business.get("day_end", "20:00")
    try:
        weekday = _dt.strptime(target_date, "%Y-%m-%d").weekday()
        day_names = {_nrm(_DAY_GR[weekday]), _nrm(_DAY_EN[weekday])}
        hours = business.get("hours", {}) or {}
        if isinstance(hours, str):
            hours = json.loads(hours or "{}")
        for k, v in hours.items():
            if _nrm(k) in day_names:
                vn = _nrm(v)
                if vn.startswith("κλειστ") or vn == "closed":
                    return ds, de, True
                parts = re.split(r'\s*-\s*', str(v).strip())
                if len(parts) == 2:
                    return parts[0].strip(), parts[1].strip(), False
    except Exception:
        pass
    return ds, de, False


def get_available_slots(slug: str, target_date: str, duration: int,
                        day_start: str, day_end: str, slot_minutes: int,
                        closed_days: list = None, blocked_dates: list = None) -> list:
    """Returns available time slots.
    closed_days  — list of ints 0=Mon … 6=Sun that are always closed.
    blocked_dates — list of 'YYYY-MM-DD' strings manually blocked by the owner.
    Returns [] immediately if the target date falls on a closed/blocked day.
    """
    from datetime import datetime, timedelta
    # ── Closed / blocked day check ────────────────────────────────────
    if closed_days or blocked_dates:
        dt = datetime.strptime(target_date, "%Y-%m-%d")
        if closed_days and dt.weekday() in closed_days:
            return []
        if blocked_dates and target_date in blocked_dates:
            return []
    # ── Normal slot computation ───────────────────────────────────────
    start    = datetime.strptime(day_start, "%H:%M")
    end      = datetime.strptime(day_end,   "%H:%M")
    existing = get_bookings_for_date(slug, target_date)
    occupied = []
    for b in existing:
        b_start = datetime.strptime(b["time"], "%H:%M")
        b_end   = b_start + timedelta(minutes=b["duration"])
        occupied.append((b_start, b_end))
    # ── 2-way: αφαίρεσε & τις ΠΡΑΓΜΑΤΙΚΕΣ πιασμένες ώρες του Google Calendar ──
    # Fail-safe: χωρίς συνδεδεμένο ημερολόγιο = no-op (ίδια συμπεριφορά με πριν).
    try:
        _biz = get_business(slug) or {}
        _gcal = (_biz.get("google_calendar_id") or "").strip()
        if _gcal:
            from calendar_service import list_events as _list_events
            for _ev in _list_events(_gcal, target_date, target_date):
                if _ev.get("all_day"):
                    return []  # ολοήμερο event (άδεια/αργία) → η μέρα είναι κλειστή
                _st, _en = _ev.get("start", ""), _ev.get("end", "")
                if "T" in _st and "T" in _en:
                    try:
                        _gs = datetime.strptime(_st[11:16], "%H:%M")
                        _ge = datetime.strptime(_en[11:16], "%H:%M")
                        if _ge > _gs:
                            occupied.append((_gs, _ge))
                    except Exception:
                        continue
    except Exception as _ce:
        print(f"[get_available_slots] gcal merge skipped: {_ce}")
    # Φίλτρο περασμένων ωρών όταν η ζητούμενη μέρα είναι ΣΗΜΕΡΑ (app.py ορίζει TZ=Europe/Athens)
    cutoff = None
    try:
        now = datetime.now()
        if target_date == now.strftime("%Y-%m-%d"):
            cutoff = datetime.strptime((now + timedelta(minutes=15)).strftime("%H:%M"), "%H:%M")
    except Exception:
        cutoff = None
    slots, current = [], start
    while current + timedelta(minutes=duration) <= end:
        slot_end = current + timedelta(minutes=duration)
        if cutoff is not None and current < cutoff:
            current += timedelta(minutes=slot_minutes)
            continue
        if all(slot_end <= os or current >= oe for os, oe in occupied):
            slots.append(current.strftime("%H:%M"))
        current += timedelta(minutes=slot_minutes)
    return slots


import threading as _threading
_BOOKING_LOCKS: dict = {}
_BOOKING_LOCKS_GUARD = _threading.Lock()


def _booking_lock(slug: str):
    """Per-slug lock: σειριοποιεί ΜΟΝΟ τις ταυτόχρονες κρατήσεις του ΙΔΙΟΥ ιατρείου (άλλα ιατρεία δεν
    μπλοκάρουν). Κλείνει το TOCTOU του app-level overlap check: το DB unique index πιάνει ΜΟΝΟ ακριβή
    (slug,date,time)· δύο overlapping-αλλά-διαφορετικής-ώρας κρατήσεις (10:00/60' + 10:30/30') θα
    περνούσαν και οι δύο τον έλεγχο χωρίς το lock."""
    with _BOOKING_LOCKS_GUARD:
        lk = _BOOKING_LOCKS.get(slug)
        if lk is None:
            lk = _threading.Lock()
            _BOOKING_LOCKS[slug] = lk
        return lk


def create_booking(slug: str, date, time, service, duration, name, phone, email="", notes="") -> int:
    """Insert a new booking.  Raises ValueError('double_booking') if the slot is already taken.

    Also auto-enqueues Outbound Proactive AI jobs (Feature #3, 2026-05-28):
    pre_arrival_upsell, no_show_reminder, review_request. Non-blocking — failure
    is logged but doesn't prevent the booking."""
    from datetime import datetime as _dt, timedelta as _td
    # ── Validate date/time: reject malformed or past slots (LLM can hallucinate) ──
    try:
        _slot_dt = _dt.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        raise ValueError("invalid_datetime")
    if _slot_dt < _dt.now() - _td(minutes=2):
        raise ValueError("past_date")
    # per-slug lock γύρω από ΟΛΟ το check→insert → ατομικός overlap guard (όχι μόνο exact-time index)
    with _booking_lock(slug), _conn() as conn:
        # ── Double-booking guard (app-level overlap check) ────────────────────
        existing = conn.execute(
            "SELECT id, time, duration FROM bookings WHERE slug=? AND date=? AND status!='cancelled'",
            (slug, date)
        ).fetchall()
        new_s = _dt.strptime(time, "%H:%M")
        new_e = new_s + _td(minutes=duration)
        for row in existing:
            ex_s = _dt.strptime(row["time"], "%H:%M")
            ex_e = ex_s + _td(minutes=row["duration"])
            if new_s < ex_e and new_e > ex_s:          # overlap
                raise ValueError("double_booking")
        # ── 2-way: μην κλείσεις πάνω σε ΠΡΑΓΜΑΤΙΚΟ event του Google Calendar ──
        # Fail-safe: χωρίς συνδεδεμένο ημερολόγιο = no-op.
        try:
            _biz = get_business(slug) or {}
            _gcal = (_biz.get("google_calendar_id") or "").strip()
            if _gcal:
                from calendar_service import list_events as _le
                for _ev in _le(_gcal, date, date):
                    if _ev.get("all_day"):
                        raise ValueError("double_booking")
                    _st, _en = _ev.get("start", ""), _ev.get("end", "")
                    if "T" in _st and "T" in _en:
                        _gs = _dt.strptime(_st[11:16], "%H:%M")
                        _ge = _dt.strptime(_en[11:16], "%H:%M")
                        if new_s < _ge and new_e > _gs:
                            raise ValueError("double_booking")
        except ValueError:
            raise
        except Exception as _ce:
            print(f"[create_booking] gcal conflict check skipped: {_ce}")
        # ── Safe to insert (DB unique index is the atomic backstop) ───────────
        try:
            cur = conn.execute(
                "INSERT INTO bookings (slug,date,time,service,duration,name,phone,email,notes) VALUES (?,?,?,?,?,?,?,?,?)",
                (slug, date, time, service, duration, name, phone, email, notes)
            )
        except sqlite3.IntegrityError:
            raise ValueError("double_booking")
        conn.commit()
        booking_id = cur.lastrowid

    # Auto-enqueue Outbound Proactive AI jobs (non-blocking)
    try:
        from voice.outbound import enqueue_jobs_for_new_booking
        enqueue_jobs_for_new_booking(slug, booking_id, {
            "date": date, "time": time, "service": service,
            "name": name, "phone": phone, "email": email,
        })
    except Exception as _e:
        print(f"[create_booking] outbound enqueue skipped: {_e}")

    # Google Calendar sync — SINGLE SOURCE OF TRUTH για ΟΛΑ τα paths (chat/voice/agentic).
    # Best-effort & non-blocking· no-op αν δεν υπάρχουν GOOGLE creds (add_to_calendar→None).
    def _sync_calendar():
        try:
            from calendar_service import add_to_calendar
            _biz = get_business(slug) or {}
            eid = add_to_calendar({"date": date, "time": time, "service": service,
                                   "duration": duration, "name": name,
                                   "phone": phone, "email": email},
                                  calendar_id=_biz.get("google_calendar_id"))
            if eid:
                set_gcal_event_id(booking_id, eid)
        except Exception as _ce:
            print(f"[create_booking] calendar sync skipped: {_ce}")
    try:
        import threading as _th
        _th.Thread(target=_sync_calendar, daemon=True).start()
    except Exception:
        pass

    # Recovered-€ Ledger — honest attribution (best-effort, non-blocking).
    # Logs ONLY when a previously-LOST patient re-books — never a normal new booking.
    try:
        import threading as _th2
        _src = "voice" if "[VOICE]" in (notes or "") else "booking"
        _th2.Thread(target=_attribute_recovery,
                    args=(slug, booking_id, phone, name, duration, _src),
                    daemon=True).start()
    except Exception:
        pass

    return booking_id


# ════════════════════════════════════════════════════════════════════
#  RECOVERED-REVENUE LEDGER — proof-of-value (honest, conservative attribution)
# ════════════════════════════════════════════════════════════════════

def _appt_value(slug: str, duration: int = 0) -> float:
    """Conservative € value of one recovered appointment (override per business)."""
    biz = get_business(slug) or {}
    try:
        return float(biz.get("avg_appointment_value") or 40.0)
    except (ValueError, TypeError):
        return 40.0


def log_recovery(slug, recovery_type, amount_eur, patient_name="", patient_phone="",
                 booking_id=None, basis="", source="") -> Optional[int]:
    """Append a recovered-€ event. Idempotent per booking_id (one recovery / re-booking)."""
    try:
        with _conn() as conn:
            cur = conn.execute(
                "INSERT INTO recovery_ledger (slug,booking_id,patient_name,patient_phone,"
                "recovery_type,amount_eur,basis,source) VALUES (?,?,?,?,?,?,?,?)",
                (slug, booking_id, patient_name, patient_phone, recovery_type,
                 float(amount_eur or 0), basis, source))
            conn.commit()
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # already logged for this booking
    except Exception as e:
        print(f"[recovery_ledger] log skipped: {e}")
        return None


def _attribute_recovery(slug, booking_id, phone, name, duration, source):
    """HONEST, conservative attribution: log a recovery ONLY when a previously-LOST
    patient re-books — prior no-show, OR lapsed patient the AI actively chased
    (win_back/no_show_recovery outbound job). NEVER counts a normal new booking.
    Amount = ONE conservative appointment value (not LTV)."""
    try:
        if not phone:
            return
        with _conn() as conn:
            ns = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE slug=? AND phone=? AND status='no_show' AND id!=?",
                (slug, phone, booking_id or -1)).fetchone()
            had_noshow = bool(ns and ns[0])
            wb = conn.execute(
                "SELECT job_type FROM outbound_jobs WHERE slug=? AND customer_phone=? "
                "AND job_type IN ('win_back','no_show_recovery') ORDER BY id DESC LIMIT 1",
                (slug, phone)).fetchone()
        rtype, basis = None, ""
        if had_noshow:
            rtype = "no_show_recovery"
            basis = "re-booked after a prior no-show" + (" + AI outreach" if wb else "")
        elif wb:
            rtype = "winback" if wb["job_type"] == "win_back" else "no_show_recovery"
            basis = f"re-booked after AI {wb['job_type']} outreach"
        if not rtype:
            return  # normal new patient — NOT a recovery, do not count (honest)
        log_recovery(slug, rtype, _appt_value(slug, duration), patient_name=name,
                     patient_phone=phone, booking_id=booking_id, basis=basis, source=source)
    except Exception as e:
        print(f"[recovery_ledger] attribution skipped: {e}")


def get_recovery_ledger(slug, days=30, limit=200) -> list:
    """Append-only list of recovered-€ events (most recent first)."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM recovery_ledger WHERE slug=? AND created>=? ORDER BY created DESC LIMIT ?",
            (slug, since, limit)).fetchall()
    return [dict(r) for r in rows]


def get_recovery_summary(slug, days=30) -> dict:
    """Headline proof: total € recovered + count + breakdown, last `days`."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(amount_eur),0) total FROM recovery_ledger "
            "WHERE slug=? AND created>=?", (slug, since)).fetchone()
        by = conn.execute(
            "SELECT recovery_type, COUNT(*) n, COALESCE(SUM(amount_eur),0) total FROM recovery_ledger "
            "WHERE slug=? AND created>=? GROUP BY recovery_type", (slug, since)).fetchall()
    return {"slug": slug, "days": days,
            "count": int(row["n"] or 0), "total_eur": round(float(row["total"] or 0)),
            "by_type": [dict(b) for b in by]}


def get_voice_agent(business_slug: str) -> Optional[dict]:
    """Returns voice_agent row for a business (for outbound calling)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM voice_agents WHERE business_slug=?",
            (business_slug,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    # Backwards-compat alias used in voice/outbound.py
    d["vapi_phone_number_id"] = d.get("vapi_phone_id")
    return d


def get_all_bookings(slug: str, limit=200) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE slug=? ORDER BY date DESC, time DESC LIMIT ?",
            (slug, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def get_weekly_stats(slug: str, week_start: str, week_end: str) -> dict:
    """Στατιστικά για μια εβδομάδα (week_start/end: YYYY-MM-DD)."""
    with _conn() as conn:
        # Συνολικά ραντεβού
        total = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE slug=? AND date>=? AND date<=? AND status!='cancelled'",
            (slug, week_start, week_end)
        ).fetchone()[0]

        # Ακυρωμένα
        cancelled = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE slug=? AND date>=? AND date<=? AND status='cancelled'",
            (slug, week_start, week_end)
        ).fetchone()[0]

        # Έσοδα (sum price) — column may not exist on older schemas
        try:
            revenue = conn.execute(
                "SELECT COALESCE(SUM(price),0) FROM bookings WHERE slug=? AND date>=? AND date<=? AND status!='cancelled'",
                (slug, week_start, week_end)
            ).fetchone()[0]
        except sqlite3.OperationalError:
            revenue = 0

        # Δημοφιλέστερη υπηρεσία
        top_row = conn.execute(
            "SELECT service, COUNT(*) as cnt FROM bookings WHERE slug=? AND date>=? AND date<=? AND status!='cancelled' GROUP BY service ORDER BY cnt DESC LIMIT 1",
            (slug, week_start, week_end)
        ).fetchone()
        top_service = dict(top_row) if top_row else None

        # Νέοι πελάτες (πρώτη εμφάνιση στη βάση αυτή την εβδομάδα)
        new_clients = conn.execute(
            """SELECT COUNT(DISTINCT phone) FROM bookings
               WHERE slug=? AND date>=? AND date<=? AND status!='cancelled'
               AND phone NOT IN (
                 SELECT DISTINCT phone FROM bookings
                 WHERE slug=? AND date<? AND status!='cancelled'
               )""",
            (slug, week_start, week_end, slug, week_start)
        ).fetchone()[0]

        # Προηγούμενη εβδομάδα για σύγκριση
        from datetime import datetime, timedelta
        ws = datetime.strptime(week_start, "%Y-%m-%d")
        prev_start = (ws - timedelta(days=7)).strftime("%Y-%m-%d")
        prev_end   = (ws - timedelta(days=1)).strftime("%Y-%m-%d")
        prev_total = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE slug=? AND date>=? AND date<=? AND status!='cancelled'",
            (slug, prev_start, prev_end)
        ).fetchone()[0]

        # Memory stats: επανερχόμενοι πελάτες (interaction_count >= 2)
        try:
            memory_total = conn.execute(
                "SELECT COUNT(*) FROM customer_memory WHERE slug=?", (slug,)
            ).fetchone()[0]
            memory_returning = conn.execute(
                "SELECT COUNT(*) FROM customer_memory WHERE slug=? AND interaction_count>=2", (slug,)
            ).fetchone()[0]
        except Exception:
            memory_total = 0
            memory_returning = 0

        # Compliance: consent records this week
        try:
            consent_week = conn.execute(
                "SELECT COUNT(*) FROM consent_logs WHERE slug=? AND created>=? AND created<=?",
                (slug, week_start, week_end + " 23:59:59")
            ).fetchone()[0]
        except Exception:
            consent_week = 0

    return {
        "total":            total,
        "cancelled":        cancelled,
        "revenue":          float(revenue),
        "top_service":      top_service,
        "new_clients":      new_clients,
        "prev_total":       prev_total,
        "week_start":       week_start,
        "week_end":         week_end,
        "memory_total":     memory_total,
        "memory_returning": memory_returning,
        "consent_week":     consent_week,
    }


def get_booking(booking_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    return dict(row) if row else None


def set_gcal_event_id(booking_id: int, event_id: str):
    """Αποθηκεύει το Google Calendar event id ώστε να μπορεί να διαγραφεί σε ακύρωση."""
    if not event_id:
        return
    with _conn() as conn:
        conn.execute("UPDATE bookings SET gcal_event_id=? WHERE id=?", (event_id, booking_id))
        conn.commit()


def cancel_booking(booking_id: int):
    eid, slug = None, None
    with _conn() as conn:
        try:
            row = conn.execute("SELECT gcal_event_id, slug FROM bookings WHERE id=?", (booking_id,)).fetchone()
            eid = row["gcal_event_id"] if row else None
            slug = row["slug"] if row else None
        except Exception:
            eid = None
        conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
        conn.commit()
    # Καθάρισε το event από το ΗΜΕΡΟΛΟΓΙΟ ΤΟΥ ΓΙΑΤΡΟΥ (per-ιατρείο) — όχι ghost ραντεβού
    if eid:
        try:
            from calendar_service import delete_from_calendar
            cal_id = (get_business(slug) or {}).get("google_calendar_id") if slug else None
            delete_from_calendar(eid, calendar_id=cal_id)
        except Exception as e:
            print(f"[cancel] calendar delete failed: {e}")


def find_bookings_by_phone(slug: str, phone: str) -> list:
    today = date.today().strftime("%Y-%m-%d")
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, date, time, service, status FROM bookings
               WHERE slug=? AND phone=? AND date>=? AND status!='cancelled'
               ORDER BY date ASC, time ASC LIMIT 5""",
            (slug, phone, today)
        ).fetchall()
    return [dict(r) for r in rows]


def reschedule_booking(booking_id: int, phone: str, new_date: str, new_time: str) -> bool:
    """Μετακινεί ραντεβού. Raises ValueError('double_booking') αν η νέα ώρα τέμνεται με άλλο."""
    from datetime import datetime as _dt, timedelta as _td
    # pre-fetch slug για να κλειδώσουμε per-slug (ίδιος ατομικός overlap guard με το create_booking)
    with _conn() as _pc:
        _pre = _pc.execute("SELECT slug FROM bookings WHERE id=? AND phone=? AND status='confirmed'",
                           (booking_id, phone)).fetchone()
    if not _pre:
        return False
    with _booking_lock(_pre["slug"]), _conn() as conn:
        row = conn.execute(
            "SELECT slug, duration FROM bookings WHERE id=? AND phone=? AND status='confirmed'",
            (booking_id, phone)
        ).fetchone()
        if not row:
            return False
        new_s = _dt.strptime(new_time, "%H:%M")
        new_e = new_s + _td(minutes=row["duration"])
        # Overlap guard (εξαιρώντας το ίδιο booking)
        existing = conn.execute(
            "SELECT time, duration FROM bookings WHERE slug=? AND date=? AND status!='cancelled' AND id!=?",
            (row["slug"], new_date, booking_id)
        ).fetchall()
        for ex in existing:
            ex_s = _dt.strptime(ex["time"], "%H:%M")
            ex_e = ex_s + _td(minutes=ex["duration"])
            if new_s < ex_e and new_e > ex_s:
                raise ValueError("double_booking")
        # Στοιχεία για re-sync ημερολογίου (πριν το update)
        full = conn.execute(
            "SELECT slug, service, duration, name, email, gcal_event_id FROM bookings WHERE id=?",
            (booking_id,)).fetchone()
        # Ο app-level overlap guard πιο πάνω είναι μη-ατομικός: υπό ταυτόχρονη πίεση δύο reschedules
        # μπορεί και τα δύο να δουν «καθαρό» και να γράψουν το ίδιο slot. Το DB unique index είναι
        # ο ατομικός backstop — μετατρέπουμε το IntegrityError στο ΙΔΙΟ καθαρό ValueError('double_booking')
        # με το create_booking, ώστε ο caller (chat/voice/assistant) να το χειρίζεται ομοιόμορφα.
        try:
            cur = conn.execute(
                """UPDATE bookings SET date=?, time=?, reminder_24=0, reminder_4=0
                   WHERE id=? AND phone=? AND status='confirmed'""",
                (new_date, new_time, booking_id, phone)
            )
        except sqlite3.IntegrityError:
            raise ValueError("double_booking")
        conn.commit()
        ok = cur.rowcount > 0
    # Calendar RE-SYNC (όχι ghost στο παλιό ώρα): σβήσε παλιό event + φτιάξε νέο στη νέα ώρα.
    if ok and full:
        def _resync():
            try:
                from calendar_service import add_to_calendar, delete_from_calendar
                cal_id = (get_business(full["slug"]) or {}).get("google_calendar_id")
                if full["gcal_event_id"]:
                    delete_from_calendar(full["gcal_event_id"], calendar_id=cal_id)
                eid = add_to_calendar({"id": booking_id, "date": new_date, "time": new_time,
                                       "service": full["service"], "duration": full["duration"],
                                       "name": full["name"], "phone": phone, "email": full["email"]},
                                      calendar_id=cal_id)
                if eid:
                    set_gcal_event_id(booking_id, eid)
            except Exception as _e:
                print(f"[reschedule] calendar resync skipped: {_e}")
        try:
            import threading as _th
            _th.Thread(target=_resync, daemon=True).start()
        except Exception:
            pass
    return ok


def cancel_booking_by_phone(phone: str, booking_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE bookings SET status='cancelled' WHERE id=? AND phone=? AND status='confirmed'",
            (booking_id, phone)
        )
        conn.commit()
        return cur.rowcount > 0


def get_pending_reminders(hours: int) -> list:
    from datetime import datetime, timedelta
    now    = datetime.now()
    target = now + timedelta(hours=hours)
    ws     = (target - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
    we     = (target + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
    col    = "reminder_24" if hours == 24 else "reminder_4"
    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT b.*, bs.smtp_email, bs.smtp_password, bs.name as business_name,
                   bs.phone as business_phone, bs.address as business_address, bs.slug
            FROM bookings b
            JOIN businesses bs ON b.slug = bs.slug
            WHERE datetime(b.date || ' ' || b.time) BETWEEN ? AND ?
              AND b.status='confirmed' AND b.{col}=0
        """, (ws, we)).fetchall()
    return [dict(r) for r in rows]


def mark_reminder_sent(booking_id: int, hours: int) -> bool:
    """Atomically marks reminder as sent. Returns True only if it wasn't already sent."""
    col = "reminder_24" if hours == 24 else "reminder_4"
    with _conn() as conn:
        cur = conn.execute(
            f"UPDATE bookings SET {col}=1 WHERE id=? AND {col}=0", (booking_id,)
        )
        conn.commit()
        return cur.rowcount > 0


def mark_risk_reminder_sent(booking_id: int) -> bool:
    """Atomic mark για το risk-based escalated reminder (μία φορά ανά booking)."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE bookings SET reminder_risk=1 WHERE id=? AND reminder_risk=0", (booking_id,))
        conn.commit()
        return cur.rowcount > 0


# ── Booking outcome (no-show detection — Outbound Layer P2) ─────────────────

def get_bookings_awaiting_outcome(grace_minutes: int = 120,
                                  lookback_hours: int = 48) -> list:
    """Confirmed ραντεβού που η ώρα τους (+ διάρκεια + grace) πέρασε χωρίς
    καταγεγραμμένο αποτέλεσμα. Κοιτάει μόνο το lookback παράθυρο — όχι ιστορικά."""
    from datetime import datetime, timedelta
    now   = datetime.now()
    since = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE status='confirmed' AND date>=? AND date<=?",
            (since, today)).fetchall()
    out = []
    for r in rows:
        b = dict(r)
        try:
            end = (datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M")
                   + timedelta(minutes=int(b.get("duration") or 30) + grace_minutes))
        except (ValueError, TypeError):
            continue
        if end <= now:
            out.append(b)
    return out


def set_booking_pending_outcome(booking_id: int) -> bool:
    """Atomic μετάβαση confirmed → pending_outcome (race-safe, μία φορά)."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE bookings SET status='pending_outcome' WHERE id=? AND status='confirmed'",
            (booking_id,))
        conn.commit()
        return cur.rowcount > 0


def mark_booking_outcome(booking_id: int, outcome: str) -> bool:
    """outcome: 'completed' ή 'no_show'. Atomic — μόνο από confirmed/pending_outcome."""
    if outcome not in ("completed", "no_show"):
        return False
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE bookings SET status=? WHERE id=? AND status IN ('confirmed','pending_outcome')",
            (outcome, booking_id))
        conn.commit()
        return cur.rowcount > 0


def get_stale_pending_outcomes(hours_since_end: int = 20, lookback_hours: int = 72) -> list:
    """Bookings σε pending_outcome όπου πέρασαν >hours_since_end από το ΤΕΛΟΣ του
    ραντεβού χωρίς ο owner να απαντήσει 'ήρθε/δεν ήρθε'. Υποψήφια για auto no-show
    inference. Συντηρητικό window (default 20h) ώστε ο owner να προλάβει να πατήσει —
    το owner tap πάντα overrides μέσα στο παράθυρο."""
    from datetime import datetime, timedelta
    now   = datetime.now()
    since = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE status='pending_outcome' AND date>=? AND date<=?",
            (since, today)).fetchall()
    out = []
    for r in rows:
        b = dict(r)
        try:
            end = (datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M")
                   + timedelta(minutes=int(b.get("duration") or 30)))
        except (ValueError, TypeError):
            continue
        if end + timedelta(hours=hours_since_end) <= now:
            out.append(b)
    return out


# ── Proof-of-value ROI (retention + pricing-power engine) ───────────────────

def _service_price_map(business):
    """({service_name: price}, avg_value) από τις υπηρεσίες της επιχείρησης."""
    svc, prices = {}, []
    for s in (business.get("services") or []):
        if isinstance(s, dict):
            try:
                p = float(s.get("price") or 0)
            except (TypeError, ValueError):
                p = 0
            if p > 0:
                svc[s.get("name")] = p
                prices.append(p)
    avg = round(sum(prices) / len(prices), 2) if prices else 40.0
    return svc, avg


def recovered_events(slug: str, days: int = 90) -> list:
    """ΠΡΑΓΜΑΤΙΚΑ recovered ραντεβού (attribution, conservative):
    ασθενής που έλαβε no_show_recovery/win_back (status='done') ΚΑΙ ξαναέκλεισε ραντεβού
    (booking.created ΜΕΤΑ το μήνυμα, εντός 30 ημερών). Κάθε rebooking μετριέται μία φορά.
    ΤΙΜΙΟΤΗΤΑ: μετράμε ΜΟΝΟ jobs που όντως στάλθηκαν — jobs με result LIKE 'skipped%'
    (π.χ. skipped_no_consent: παραλείφθηκαν χωρίς να σταλεί τίποτα) ΕΞΑΙΡΟΥΝΤΑΙ, ώστε το
    ledger να μην αποδίδει ποτέ ανάκτηση σε μήνυμα που δεν έφυγε (θα εξέθετε τον γιατρό).
    Επιστρέφει [{phone, type, service, value, month}]. Αυτό είναι το αληθινό 'Recovered-Revenue Ledger'."""
    from datetime import datetime, timedelta
    since_ts = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    business = get_business(slug) or {}
    svc_price, avg_value = _service_price_map(business)
    events, seen = [], set()
    with _conn() as conn:
        try:
            jobs = conn.execute(
                "SELECT customer_phone, job_type, created FROM outbound_jobs "
                "WHERE slug=? AND job_type IN ('no_show_recovery','win_back') "
                "AND status='done' AND result NOT LIKE 'skipped%' AND created>=? "
                "ORDER BY created", (slug, since_ts)).fetchall()
        except Exception:
            jobs = []
        for j in jobs:
            ph = j["customer_phone"]
            if not ph:
                continue
            # Cross-channel match: voice δίνει E.164 (+30690…), web δίνει bare (690…).
            # Ταιριάζουμε στα τελευταία 10 ψηφία (suffix), strip-άροντας μη-ψηφία και στις 2 πλευρές.
            bare = "".join(ch for ch in ph if ch.isdigit())[-10:]
            try:
                if len(bare) >= 9:
                    row = conn.execute(
                        "SELECT name, service, created FROM bookings WHERE slug=? "
                        "AND status IN ('confirmed','completed') AND created > ? "
                        "AND created <= datetime(?, '+30 days') "
                        "AND replace(replace(replace(replace(replace(phone,' ',''),'-',''),'+',''),'(',''),')','') "
                        "LIKE '%' || ? ORDER BY created LIMIT 1",
                        (slug, j["created"], j["created"], bare)).fetchone()
                else:
                    row = conn.execute(
                        "SELECT name, service, created FROM bookings WHERE slug=? AND phone=? "
                        "AND status IN ('confirmed','completed') AND created > ? "
                        "AND created <= datetime(?, '+30 days') ORDER BY created LIMIT 1",
                        (slug, ph, j["created"], j["created"])).fetchone()
            except Exception:
                row = None
            if not row:
                continue
            key = (bare, row["created"])         # dedup σε NORMALIZED τηλέφωνο: ίδιο rebooking
                                                 # σε διαφορετικά formats (voice +30.. vs web ..) = ΜΙΑ φορά
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "phone": ph, "name": (row["name"] or "").strip(), "type": j["job_type"],
                "service": row["service"],
                "value": svc_price.get(row["service"], avg_value),
                "month": (row["created"] or "")[:7],
            })
    return events


def recovered_revenue(slug: str, days: int = 90) -> dict:
    """Σύνοψη του Recovered-Revenue Ledger: συνολικά € + count + breakdown ανά τύπο."""
    ev = recovered_events(slug, days)
    by_type = {}
    for e in ev:
        by_type[e["type"]] = round(by_type.get(e["type"], 0) + e["value"], 2)
    return {
        "recovered_eur": round(sum(e["value"] for e in ev), 2),
        "recovered_count": len(ev),
        "by_type": by_type,
        "period_days": days,
    }


def roi_summary(slug: str, days: int = 30) -> dict:
    """«Πόσα € σου έσωσε το AI» — conservative, μετράει μόνο αποδεδειγμένες ενέργειες.
    Τροφοδοτεί το dashboard που δικαιολογεί το premium tier + κόβει το churn."""
    from datetime import datetime, timedelta
    since_dt = datetime.now() - timedelta(days=days)
    since    = since_dt.strftime("%Y-%m-%d")
    since_ts = since_dt.strftime("%Y-%m-%d %H:%M:%S")

    business = get_business(slug) or {}
    prices = []
    for s in (business.get("services") or []):
        if isinstance(s, dict):
            try:
                p = float(s.get("price") or 0)
                if p > 0:
                    prices.append(p)
            except (TypeError, ValueError):
                pass
    avg_value = round(sum(prices) / len(prices), 2) if prices else 40.0

    with _conn() as conn:
        def _one(sql, params):
            try:
                return conn.execute(sql, params).fetchone()[0] or 0
            except Exception:
                return 0
        answered_calls = _one(
            "SELECT COUNT(*) FROM voice_calls WHERE business_slug=? AND started_at>=?",
            (slug, since_ts))
        ai_bookings = _one(
            "SELECT COUNT(*) FROM bookings WHERE slug=? AND date>=? "
            "AND status!='cancelled' AND notes LIKE '%[VOICE]%'", (slug, since))
        noshow_recovery = _one(
            "SELECT COUNT(*) FROM outbound_jobs WHERE slug=? AND job_type='no_show_recovery' "
            "AND status='done' AND created>=?", (slug, since_ts))
        reminders_sent = _one(
            "SELECT COUNT(*) FROM bookings WHERE slug=? AND date>=? "
            "AND (reminder_24=1 OR reminder_4=1)", (slug, since))

    euros_saved   = round(ai_bookings * avg_value, 2)
    # RECOVERED = ΠΡΑΓΜΑΤΙΚΟ attribution (ασθενείς που ξαναέκλεισαν μετά από recovery/winback),
    # ΟΧΙ μηνύματα×μέση_αξία. Το ειλικρινές 'Recovered-Revenue Ledger' (board 17/6).
    rec = recovered_revenue(slug, days=days)   # ίδιο παράθυρο με το 'contacted' (συνέπεια στο dashboard)
    recovered_eur   = rec["recovered_eur"]
    recovered_count = rec["recovered_count"]
    return {
        "period_days":          days,
        "answered_calls":       answered_calls,
        "ai_bookings":          ai_bookings,
        "noshow_recovery_sent": noshow_recovery,     # πόσους κυνηγήσαμε (contacted)
        "recovered_count":      recovered_count,     # πόσοι ΟΝΤΩΣ ξαναέκλεισαν (attributed)
        "recovered_eur":        recovered_eur,        # πραγματικά € που γύρισαν
        "recovered_by_type":    rec["by_type"],
        "reminders_sent":       reminders_sent,
        "avg_session_value":    avg_value,
        "euros_saved":          euros_saved,
        "headline":             f"Το AI έκλεισε {ai_bookings} ραντεβού = €{euros_saved:.0f} σε {days} μέρες",
        "recovery_headline":    f"Ανακτήθηκαν {recovered_count} ραντεβού = €{recovered_eur:.0f} (επιβεβαιωμένες επανακρατήσεις)",
    }


def roi_timeseries(slug: str, months: int = 6) -> dict:
    """Monthly buckets για το live €-dashboard (Chart.js): bookings, εκτιμώμενα έσοδα,
    no-shows, recovered ανά μήνα + cumulative all-time totals. Συντηρητικός υπολογισμός
    (μέση αξία συνεδρίας × bookings). Τροφοδοτεί τα γραφήματα + το headline 'recovered €'."""
    from datetime import datetime
    business = get_business(slug) or {}
    prices = []
    for s in (business.get("services") or []):
        if isinstance(s, dict):
            try:
                p = float(s.get("price") or 0)
                if p > 0:
                    prices.append(p)
            except (TypeError, ValueError):
                pass
    avg_value = round(sum(prices) / len(prices), 2) if prices else 40.0

    now = datetime.now()
    buckets = []
    for i in range(months - 1, -1, -1):
        y, m = now.year, now.month - i
        while m <= 0:
            m += 12
            y -= 1
        buckets.append(f"{y:04d}-{m:02d}")
    series = {k: {"month": k, "bookings": 0, "revenue": 0.0, "no_shows": 0,
                  "recovered": 0, "recovered_eur": 0.0} for k in buckets}
    earliest = buckets[0] + "-01"

    with _conn() as conn:
        def _rows(sql, params):
            try:
                return conn.execute(sql, params).fetchall()
            except Exception:
                return []
        for row in _rows("SELECT substr(date,1,7) ym, COUNT(*) n FROM bookings "
                         "WHERE slug=? AND status!='cancelled' AND date>=? GROUP BY ym", (slug, earliest)):
            if row["ym"] in series:
                series[row["ym"]]["bookings"] = row["n"]
                series[row["ym"]]["revenue"] = round(row["n"] * avg_value, 2)
        for row in _rows("SELECT substr(date,1,7) ym, COUNT(*) n FROM bookings "
                         "WHERE slug=? AND status='no_show' AND date>=? GROUP BY ym", (slug, earliest)):
            if row["ym"] in series:
                series[row["ym"]]["no_shows"] = row["n"]
        # recovered ανά μήνα = ΠΡΑΓΜΑΤΙΚΕΣ επανακρατήσεις (attribution), όχι μηνύματα
        try:
            for e in recovered_events(slug, days=months * 31):
                if e["month"] in series:
                    series[e["month"]]["recovered"] += 1
                    series[e["month"]]["recovered_eur"] = round(
                        series[e["month"]].get("recovered_eur", 0) + e["value"], 2)
        except Exception:
            pass
        try:
            cum_bookings = conn.execute("SELECT COUNT(*) FROM bookings WHERE slug=? "
                                        "AND status!='cancelled'", (slug,)).fetchone()[0] or 0
        except Exception:
            cum_bookings = 0

    return {
        "months":              [series[k] for k in buckets],
        "avg_session_value":   avg_value,
        "cumulative_bookings": cum_bookings,
        "cumulative_revenue":  round(cum_bookings * avg_value, 2),
    }


# ════════════════════════════════════════════════════════════════════
#  CLIENT PROFILES
# ════════════════════════════════════════════════════════════════════

def get_client_profile(slug: str, phone: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM client_profiles WHERE slug=? AND phone=?", (slug, phone)
        ).fetchone()
    if not row:
        return None
    profile = dict(row)
    # PHI at rest: αποκρυπτογράφησε preferences/skin_notes (legacy plaintext → passthrough)
    try:
        from crypto_util import decrypt
        if profile.get("preferences"):
            profile["preferences"] = decrypt(profile["preferences"])
        if profile.get("skin_notes"):
            profile["skin_notes"] = decrypt(profile["skin_notes"])
    except Exception:
        pass
    with _conn() as conn:
        history = conn.execute(
            """SELECT service, date FROM bookings
               WHERE slug=? AND phone=? AND status='confirmed'
               ORDER BY date DESC LIMIT 10""",
            (slug, phone)
        ).fetchall()
    profile["history"] = [dict(h) for h in history]
    return profile


def upsert_client_profile(slug: str, phone: str, name: str = None, email: str = None,
                          preferences: str = None, skin_notes: str = None):
    # PHI at rest: κρυπτογράφησε preferences/skin_notes πριν την αποθήκευση (GDPR Art.9/32)
    try:
        from crypto_util import encrypt
        _pref = encrypt(preferences) if preferences else preferences
        _skin = encrypt(skin_notes) if skin_notes else skin_notes
    except Exception:
        _pref, _skin = preferences, skin_notes
    with _conn() as conn:
        existing = conn.execute(
            "SELECT id FROM client_profiles WHERE slug=? AND phone=?", (slug, phone)
        ).fetchone()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if existing:
            updates, vals = [], []
            if name:        updates.append("name=?");        vals.append(name)
            if email:       updates.append("email=?");       vals.append(email)
            if preferences: updates.append("preferences=?"); vals.append(_pref)
            if skin_notes:  updates.append("skin_notes=?");  vals.append(_skin)
            updates.append("updated=?"); vals.append(now)
            vals += [slug, phone]
            if updates:
                conn.execute(f"UPDATE client_profiles SET {', '.join(updates)} WHERE slug=? AND phone=?", vals)
        else:
            conn.execute(
                """INSERT INTO client_profiles (slug, phone, name, email, preferences, skin_notes, first_visit)
                   VALUES (?,?,?,?,?,?,?)""",
                (slug, phone, name or "", email or "", _pref or "", _skin or "",
                 datetime.now().strftime("%Y-%m-%d"))
            )
        conn.commit()


def update_client_after_booking(slug: str, phone: str, name: str, email: str, service: str, date: str):
    with _conn() as conn:
        existing = conn.execute(
            "SELECT id, first_visit FROM client_profiles WHERE slug=? AND phone=?", (slug, phone)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE client_profiles
                   SET visit_count=visit_count+1, last_visit=?, name=?, email=?, updated=datetime('now')
                   WHERE slug=? AND phone=?""",
                (date, name, email, slug, phone)
            )
        else:
            conn.execute(
                """INSERT INTO client_profiles (slug, phone, name, email, visit_count, last_visit, first_visit)
                   VALUES (?,?,?,?,1,?,?)""",
                (slug, phone, name, email, date, date)
            )
        conn.commit()


# ════════════════════════════════════════════════════════════════════
#  WAITLIST
# ════════════════════════════════════════════════════════════════════

def add_to_waitlist(slug: str, date: str, time: str, service: str,
                    name: str, phone: str, email: str = "") -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO waitlist (slug,date,time,service,name,phone,email) VALUES (?,?,?,?,?,?,?)",
            (slug, date, time, service, name, phone, email)
        )
        conn.commit()
        return cur.lastrowid


def pop_first_waiter(slug: str, date: str, time: str) -> dict | None:
    with _conn() as conn:
        # Match the exact freed slot OR a flexible "any"-time waiter (π.χ. φωνητική εγγραφή «όποτε
        # βρεθεί θέση») — αλλιώς όποιος μπήκε για «οποτεδήποτε» δεν τραβιόταν ΠΟΤΕ σε ακύρωση.
        # Προτεραιότητα στον exact-time match, μετά FIFO.
        row = conn.execute(
            "SELECT * FROM waitlist WHERE slug=? AND date=? AND (time=? OR time='any' OR time='') "
            "ORDER BY (time=?) DESC, created_at ASC LIMIT 1",
            (slug, date, time, time)
        ).fetchone()
        if not row:
            return None
        waiter = dict(row)
        conn.execute("DELETE FROM waitlist WHERE id=?", (waiter["id"],))
        conn.commit()
    return waiter


# ════════════════════════════════════════════════════════════════════
#  PROACTIVE OUTREACH
# ════════════════════════════════════════════════════════════════════

def get_clients_for_reengagement(slug: str, days: int = 45, limit: int = 15) -> list:
    from datetime import datetime, timedelta
    cutoff  = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    no_spam = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM client_profiles
            WHERE slug=? AND email!='' AND email IS NOT NULL
              AND last_visit IS NOT NULL AND last_visit<=?
              AND (last_outreach IS NULL OR last_outreach<=?)
            ORDER BY last_visit ASC LIMIT ?
        """, (slug, cutoff, no_spam, limit)).fetchall()
    return [dict(r) for r in rows]


def get_days_with_few_bookings(slug: str, days_ahead: int = 3, threshold: int = 2) -> list:
    from datetime import datetime, timedelta
    today, result = datetime.now().date(), []
    for i in range(1, days_ahead + 1):
        d = (today + timedelta(days=i)).strftime("%Y-%m-%d")
        with _conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE slug=? AND date=? AND status='confirmed'",
                (slug, d)
            ).fetchone()[0]
        if count < threshold:
            result.append(d)
    return result


def mark_outreach_sent(slug: str, phone: str):
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as conn:
        conn.execute(
            "UPDATE client_profiles SET last_outreach=? WHERE slug=? AND phone=?",
            (today, slug, phone)
        )
        conn.commit()


def get_bookings_for_questionnaire(hours_min: int = 1, hours_max: int = 3) -> list:
    """Bookings created 1-3h ago, questionnaire not yet sent, has email."""
    from datetime import datetime, timedelta
    now    = datetime.now()
    after  = (now - timedelta(hours=hours_max)).strftime("%Y-%m-%d %H:%M:%S")
    before = (now - timedelta(hours=hours_min)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        rows = conn.execute("""
            SELECT b.*, bs.smtp_email, bs.smtp_password, bs.name as business_name,
                   bs.google_review_url, bs.questionnaire, bs.accent_color, bs.slug as biz_slug
            FROM bookings b
            JOIN businesses bs ON b.slug = bs.slug
            WHERE b.created BETWEEN ? AND ?
              AND b.status='confirmed' AND b.questionnaire_sent=0
              AND b.email != '' AND b.email IS NOT NULL AND bs.active=1
        """, (after, before)).fetchall()
    return [dict(r) for r in rows]


def get_bookings_for_review(hours_after: int = 2) -> list:
    """Bookings whose appointment time passed hours_after hours ago, review not sent."""
    from datetime import datetime, timedelta
    target = (datetime.now() - timedelta(hours=hours_after)).strftime("%Y-%m-%d %H:%M")
    with _conn() as conn:
        rows = conn.execute("""
            SELECT b.*, bs.smtp_email, bs.smtp_password, bs.name as business_name,
                   bs.phone as business_phone, bs.address as business_address,
                   bs.google_review_url, bs.accent_color, bs.slug as biz_slug
            FROM bookings b
            JOIN businesses bs ON b.slug = bs.slug
            WHERE (b.date || ' ' || b.time) <= ?
              AND b.status='confirmed' AND b.review_sent=0
              AND b.email != '' AND b.email IS NOT NULL AND bs.active=1
        """, (target,)).fetchall()
    return [dict(r) for r in rows]


def mark_questionnaire_sent(booking_id: int) -> bool:
    """Atomically marks questionnaire as sent. Returns True only if it wasn't already sent."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE bookings SET questionnaire_sent=1 WHERE id=? AND questionnaire_sent=0",
            (booking_id,)
        )
        conn.commit()
        return cur.rowcount > 0


def mark_review_sent(booking_id: int) -> bool:
    """Atomically marks review as sent. Returns True only if it wasn't already sent."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE bookings SET review_sent=1 WHERE id=? AND review_sent=0",
            (booking_id,)
        )
        conn.commit()
        return cur.rowcount > 0


def get_clients_with_email(slug: str, limit: int = 20) -> list:
    from datetime import datetime, timedelta
    no_spam = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM client_profiles
            WHERE slug=? AND email!='' AND email IS NOT NULL
              AND (last_outreach IS NULL OR last_outreach<=?)
            ORDER BY last_visit DESC LIMIT ?
        """, (slug, no_spam, limit)).fetchall()
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════
#  COLD EMAIL CAMPAIGN — unsubscribes & sent log
# ════════════════════════════════════════════════════════════════════

def is_unsubscribed(email: str) -> bool:
    if not email:
        return True
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM email_unsubscribes WHERE email=? LIMIT 1",
            (email.strip().lower(),)
        ).fetchone()
        return row is not None


def add_unsubscribe(email: str, source: str = "link") -> bool:
    if not email or "@" not in email:
        return False
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO email_unsubscribes (email, source) VALUES (?, ?)",
            (email.strip().lower(), source)
        )
        conn.commit()
    return True


def list_unsubscribes() -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT email, created_at, source FROM email_unsubscribes ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def was_sent(email: str, campaign: str = "cold") -> bool:
    """Επιστρέφει True μόνο αν το email στάλθηκε ΕΠΙΤΥΧΩΣ (status='ok').
    Αποτυχημένα emails μπορούν να ξαναδοκιμαστούν."""
    if not email:
        return False
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM email_sent_log WHERE email=? AND campaign=? AND status='ok' LIMIT 1",
            (email.strip().lower(), campaign)
        ).fetchone()
        return row is not None


def mark_sent(email: str, campaign: str = "cold", status: str = "ok") -> None:
    if not email:
        return
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO email_sent_log (email, campaign, sent_at, status) "
            "VALUES (?, ?, datetime('now'), ?)",
            (email.strip().lower(), campaign, status)
        )
        conn.commit()


def count_sent(campaign: str = "cold") -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM email_sent_log WHERE campaign=? AND status='ok'",
            (campaign,)
        ).fetchone()
    return row["n"] if row else 0


# ════════════════════════════════════════════════════════════════════
#  WAITLIST AUTO-CLAIM (pending_claims)
# ════════════════════════════════════════════════════════════════════

def add_pending_claim(slug: str, date: str, time: str, service: str,
                      name: str, phone: str, email: str,
                      expires_in_hours: int = 6) -> int:
    """Καταγράφει pending claim από waitlist offer. Επιστρέφει το claim_id."""
    from datetime import datetime, timedelta
    expires_at = (datetime.now() + timedelta(hours=expires_in_hours)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO pending_claims (slug,date,time,service,name,phone,email,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (slug, date, time, service, name, phone, email, expires_at)
        )
        conn.commit()
        return cur.lastrowid


def get_pending_claim(claim_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM pending_claims WHERE id=?",
            (claim_id,)
        ).fetchone()
    return dict(row) if row else None


def mark_claim_used(claim_id: int) -> bool:
    """Ατομικά: σημειώνει το claim ως claimed=1. Αν είναι ήδη 1, επιστρέφει False."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE pending_claims SET claimed=1 WHERE id=? AND claimed=0",
            (claim_id,)
        )
        conn.commit()
        return cur.rowcount > 0


# ════════════════════════════════════════════════════════════════════
#  AUTOSALES — outreach helpers (Phase 1+2+3)
# ════════════════════════════════════════════════════════════════════

# ── Leads ───────────────────────────────────────────────────────────

def insert_lead(data: dict) -> int:
    """Idempotent insert. If email exists, returns existing lead_id (no overwrite).
    Required: business_name, source. Optional: phone, email, website, ig, fb, li, wa, viber, etc."""
    if not data.get("business_name") or not data.get("source"):
        raise ValueError("business_name and source required")
    email = (data.get("email") or "").strip().lower() or None
    with _conn() as conn:
        if email:
            existing = conn.execute(
                "SELECT id FROM outreach_leads WHERE email=?", (email,)
            ).fetchone()
            if existing:
                return existing["id"]
        cur = conn.execute("""
            INSERT INTO outreach_leads
              (source, business_name, industry, city, address, phone, email, website,
               instagram, facebook, linkedin, whatsapp, viber, notes, score, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data["source"], data["business_name"],
            data.get("industry"), data.get("city"), data.get("address"),
            data.get("phone"), email, data.get("website"),
            data.get("instagram"), data.get("facebook"), data.get("linkedin"),
            data.get("whatsapp"), data.get("viber"),
            data.get("notes"),
            int(data.get("score", 50)),
            data.get("status", "new"),
        ))
        conn.commit()
        return cur.lastrowid


def get_lead(lead_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM outreach_leads WHERE id=?", (lead_id,)).fetchone()
    return dict(row) if row else None


_LEAD_COL_BY_CHANNEL = {
    "email": "email", "instagram": "instagram", "facebook": "facebook",
    "linkedin": "linkedin", "whatsapp": "whatsapp", "viber": "viber",
    "sms": "phone", "voice": "phone",
}


def find_lead_by(channel: str, identifier: str) -> dict | None:
    col = _LEAD_COL_BY_CHANNEL.get(channel)
    if not col or not identifier:
        return None
    with _conn() as conn:
        row = conn.execute(
            f"SELECT * FROM outreach_leads WHERE {col}=? LIMIT 1", (identifier,)
        ).fetchone()
    return dict(row) if row else None


def list_leads(status: str = None, industry: str = None, city: str = None,
               q: str = None, limit: int = 100, offset: int = 0) -> dict:
    """Returns {leads:[…], total:N}."""
    where, params = [], []
    if status:   where.append("status=?");   params.append(status)
    if industry: where.append("industry=?"); params.append(industry)
    if city:     where.append("city=?");     params.append(city)
    if q:
        where.append("(business_name LIKE ? OR email LIKE ? OR website LIKE ?)")
        like = f"%{q}%"; params += [like, like, like]
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    with _conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM outreach_leads {sql_where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM outreach_leads {sql_where} "
            f"ORDER BY score DESC, created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()
    return {"leads": [dict(r) for r in rows], "total": total}


def update_lead(lead_id: int, **fields):
    if not fields:
        return
    cols = [f"{k}=?" for k in fields]
    cols.append("updated_at=datetime('now')")
    vals = list(fields.values()) + [lead_id]
    with _conn() as conn:
        conn.execute(f"UPDATE outreach_leads SET {', '.join(cols)} WHERE id=?", vals)
        conn.commit()


_GENERIC_EMAIL_PREFIXES = (
    "info@", "contact@", "admin@", "support@", "sales@", "hello@",
    "office@", "reception@", "secretariat@", "noreply@", "no-reply@",
    "mail@", "enquiries@", "inquiry@", "γραμματεια@",
)


_HOTEL_KEYWORDS = ("hotel", "ξενοδοχ", "boutique", "aparthotel", "pension", "suites", "villa", "palace", "lodge")
_CLINIC_KEYWORDS = ("dental", "οδοντίατρ", "οδοντιατρ", "vet", "κτηνίατρ", "κτηνιατρ",
                    "physio", "φυσικοθερ", "φυσιοθερ", "physical therapy",
                    "derma", "δερματολ", "doctor", "ιατρ", "clinic", "medical")


def is_hotel_or_clinic(lead: dict) -> bool:
    """True if lead.industry/category matches hotels or clinics (pivot 2026-05-28)."""
    blob = " ".join(str(lead.get(k) or "") for k in
                    ("industry", "category", "business_type", "business_name", "notes")).lower()
    return (any(k in blob for k in _HOTEL_KEYWORDS) or
            any(k in blob for k in _CLINIC_KEYWORDS))


def get_eligible_leads_for_channel(channel: str, limit: int = 50,
                                   cooldown_days: int = 90,
                                   only_hotels_and_clinics: bool = False) -> list:
    """Leads NOT blacklisted, NOT already messaged on this channel,
    last_outreach_at older than cooldown_days OR NULL.
    For email channel: ALSO skips generic addresses (info@, contact@ κλπ)
    γιατί έχουν bounce rate >40% και 0% reply rate.
    Priority: status='new' or 'enriched' first, then by score, then newest.

    Set only_hotels_and_clinics=True to filter to ONLY hotels + medical clinics
    (pivot 2026-05-28). Defaults to False για backwards compatibility."""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=cooldown_days)).strftime("%Y-%m-%d")
    col = _LEAD_COL_BY_CHANNEL.get(channel)
    if not col:
        return []

    extra_filter = ""
    if channel == "email":
        like_clauses = " AND ".join(
            f"LOWER(l.{col}) NOT LIKE '{p}%'" for p in _GENERIC_EMAIL_PREFIXES
        )
        extra_filter = f" AND {like_clauses}"

    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT l.* FROM outreach_leads l
            WHERE l.{col} IS NOT NULL AND l.{col} != ''
              AND l.status IN ('new','enriched','active')
              AND (l.last_outreach_at IS NULL OR l.last_outreach_at < ?)
              {extra_filter}
              AND NOT EXISTS (
                SELECT 1 FROM outreach_blacklist b
                WHERE b.identifier = l.{col} AND b.channel IN (?, 'all')
              )
              AND NOT EXISTS (
                SELECT 1 FROM outreach_messages m
                WHERE m.lead_id = l.id AND m.channel = ? AND m.sequence_step = 0
              )
            ORDER BY
              CASE l.status WHEN 'new' THEN 0 WHEN 'enriched' THEN 1 ELSE 2 END,
              l.score DESC, l.created_at DESC
            LIMIT ?
        """, (cutoff, channel, channel, limit * 3 if only_hotels_and_clinics else limit)).fetchall()
    leads = [dict(r) for r in rows]
    if only_hotels_and_clinics:
        leads = [ld for ld in leads if is_hotel_or_clinic(ld)][:limit]
    return leads


def quarantine_generic_emails() -> dict:
    """One-time/idempotent: marks generic emails (info@, contact@, etc.)
    as status='low_quality' ώστε να μην ξανασταλούν.
    Καλείται σε κάθε startup. Επιστρέφει stats."""
    like_clauses = " OR ".join(
        f"LOWER(email) LIKE '{p}%'" for p in _GENERIC_EMAIL_PREFIXES
    )
    with _conn() as conn:
        cur = conn.execute(f"""
            UPDATE outreach_leads
            SET status='low_quality'
            WHERE status IN ('new','enriched','active')
              AND email IS NOT NULL AND email != ''
              AND ({like_clauses})
        """)
        conn.commit()
        return {"quarantined": cur.rowcount}


# ── Messages (outbound) ─────────────────────────────────────────────

def queue_message(lead_id: int, channel: str, body: str, subject: str = None,
                  sequence_step: int = 0, scheduled_at: str = None) -> int | None:
    """Insert pending message. Returns message_id, or None if dedup conflict."""
    try:
        with _conn() as conn:
            cur = conn.execute("""
                INSERT INTO outreach_messages
                  (lead_id, channel, sequence_step, subject, body, scheduled_at, status)
                VALUES (?,?,?,?,?,?, 'pending')
            """, (lead_id, channel, sequence_step, subject, body, scheduled_at))
            conn.commit()
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def get_due_messages(channel: str, limit: int = 50) -> list:
    """Pending messages whose scheduled_at <= now (or NULL).
    Uses Python local time για να ταιριάζει με όσα γράφει η εφαρμογή
    (SQLite's datetime('now') επιστρέφει UTC — διαφορά 2-3 ώρες)."""
    from datetime import datetime
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        rows = conn.execute("""
            SELECT m.*, l.business_name, l.email AS lead_email,
                   l.instagram AS lead_ig, l.facebook AS lead_fb,
                   l.phone AS lead_phone, l.linkedin AS lead_li,
                   l.whatsapp AS lead_wa, l.viber AS lead_viber
            FROM outreach_messages m
            JOIN outreach_leads l ON l.id = m.lead_id
            WHERE m.channel=? AND m.status='pending'
              AND (m.scheduled_at IS NULL OR m.scheduled_at <= ?)
            ORDER BY m.created_at ASC LIMIT ?
        """, (channel, now_str, limit)).fetchall()
    return [dict(r) for r in rows]


def mark_message_sent(message_id: int, provider_id: str = None) -> bool:
    """Atomically: pending → sent. Also updates lead.last_outreach_at."""
    with _conn() as conn:
        cur = conn.execute("""
            UPDATE outreach_messages
            SET status='sent', sent_at=datetime('now'), provider_id=?
            WHERE id=? AND status='pending'
        """, (provider_id, message_id))
        if cur.rowcount > 0:
            conn.execute("""
                UPDATE outreach_leads
                SET last_outreach_at=datetime('now'), updated_at=datetime('now')
                WHERE id=(SELECT lead_id FROM outreach_messages WHERE id=?)
            """, (message_id,))
        conn.commit()
        return cur.rowcount > 0


def mark_message_failed(message_id: int, reason: str) -> bool:
    with _conn() as conn:
        cur = conn.execute("""
            UPDATE outreach_messages SET status='failed', failure_reason=?
            WHERE id=? AND status='pending'
        """, ((reason or "")[:500], message_id))
        conn.commit()
        return cur.rowcount > 0


def get_message_stats(channel: str = None, days: int = 7) -> dict:
    where = "WHERE created_at >= datetime('now', ?)"
    params = [f"-{days} days"]
    if channel:
        where += " AND channel=?"; params.append(channel)
    with _conn() as conn:
        total   = conn.execute(f"SELECT COUNT(*) FROM outreach_messages {where}", params).fetchone()[0]
        sent    = conn.execute(f"SELECT COUNT(*) FROM outreach_messages {where} AND status='sent'", params).fetchone()[0]
        failed  = conn.execute(f"SELECT COUNT(*) FROM outreach_messages {where} AND status='failed'", params).fetchone()[0]
        pending = conn.execute(f"SELECT COUNT(*) FROM outreach_messages {where} AND status='pending'", params).fetchone()[0]
    return {"total": total, "sent": sent, "failed": failed, "pending": pending}


# ── Replies (inbound) ───────────────────────────────────────────────

def insert_reply(lead_id: int, message_id: int | None, channel: str, raw_text: str,
                 classification: str = None, sentiment: str = None) -> int:
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO outreach_replies
              (lead_id, message_id, channel, raw_text, classification, sentiment)
            VALUES (?,?,?,?,?,?)
        """, (lead_id, message_id, channel, raw_text, classification, sentiment))
        conn.commit()
        return cur.lastrowid


def list_unhandled_replies(channel: str = None, limit: int = 50) -> list:
    where = "WHERE r.handled=0"
    params = []
    if channel:
        where += " AND r.channel=?"; params.append(channel)
    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT r.*, l.business_name, l.email AS lead_email, l.phone AS lead_phone
            FROM outreach_replies r
            LEFT JOIN outreach_leads l ON l.id = r.lead_id
            {where} ORDER BY r.received_at DESC LIMIT ?
        """, params + [limit]).fetchall()
    return [dict(r) for r in rows]


def mark_reply_handled(reply_id: int, by: str = "founder") -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE outreach_replies SET handled=1, handled_by=? WHERE id=? AND handled=0",
            (by, reply_id)
        )
        conn.commit()
        return cur.rowcount > 0


# ── Blacklist ───────────────────────────────────────────────────────

def is_blacklisted(identifier: str, channel: str = "all") -> bool:
    if not identifier:
        return True
    ident = identifier.strip().lower()
    with _conn() as conn:
        row = conn.execute("""
            SELECT 1 FROM outreach_blacklist
            WHERE identifier=? AND channel IN (?, 'all') LIMIT 1
        """, (ident, channel)).fetchone()
    return row is not None


def add_to_blacklist(identifier: str, channel: str = "all", reason: str = "manual") -> bool:
    if not identifier:
        return False
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO outreach_blacklist (identifier, channel, reason) VALUES (?,?,?)",
            (identifier.strip().lower(), channel, reason)
        )
        conn.commit()
    return True


def list_blacklist(channel: str = None) -> list:
    where, params = "", []
    if channel:
        where = "WHERE channel IN (?, 'all')"; params = [channel]
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM outreach_blacklist {where} ORDER BY created_at DESC", params
        ).fetchall()
    return [dict(r) for r in rows]


def remove_from_blacklist(identifier: str, channel: str) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM outreach_blacklist WHERE identifier=? AND channel=?",
            (identifier.strip().lower(), channel)
        )
        conn.commit()
        return cur.rowcount > 0


# ── Channel state ───────────────────────────────────────────────────

def get_channel_state(channel: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM outreach_channel_state WHERE channel=?", (channel,)
        ).fetchone()
    return dict(row) if row else None


def list_channel_states() -> list:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM outreach_channel_state ORDER BY channel").fetchall()
    return [dict(r) for r in rows]


def set_channel_paused(channel: str, hours: int, reason: str):
    from datetime import datetime, timedelta
    until = (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        conn.execute("""
            UPDATE outreach_channel_state
            SET paused_until=?, pause_reason=?, updated_at=datetime('now')
            WHERE channel=?
        """, (until, reason, channel))
        conn.commit()


def resume_channel(channel: str):
    with _conn() as conn:
        conn.execute("""
            UPDATE outreach_channel_state
            SET paused_until=NULL, pause_reason=NULL, updated_at=datetime('now')
            WHERE channel=?
        """, (channel,))
        conn.commit()


def is_channel_active(channel: str) -> tuple[bool, str]:
    """Returns (active?, reason). Auto-resumes if paused_until passed."""
    from datetime import datetime
    s = get_channel_state(channel)
    if not s:
        return False, "channel_not_configured"
    if not s["enabled"]:
        return False, "disabled"
    if s["paused_until"]:
        try:
            until = datetime.strptime(s["paused_until"], "%Y-%m-%d %H:%M:%S")
            if datetime.now() < until:
                return False, f"paused: {s['pause_reason']}"
            else:
                resume_channel(channel)
        except ValueError:
            pass
    return True, "active"


def increment_channel_sent(channel: str) -> int:
    """Increments sent_today. Auto-resets if past last daily_reset_at.
    Returns new sent_today value (or 0 if channel not configured)."""
    from datetime import date
    today = date.today().isoformat()
    with _conn() as conn:
        s = conn.execute(
            "SELECT sent_today, daily_reset_at FROM outreach_channel_state WHERE channel=?",
            (channel,)
        ).fetchone()
        if not s:
            return 0
        if s["daily_reset_at"] != today:
            conn.execute("""
                UPDATE outreach_channel_state
                SET sent_today=1, daily_reset_at=?, last_send_at=datetime('now'),
                    updated_at=datetime('now')
                WHERE channel=?
            """, (today, channel))
            conn.commit()
            return 1
        conn.execute("""
            UPDATE outreach_channel_state
            SET sent_today=sent_today+1, last_send_at=datetime('now'),
                updated_at=datetime('now')
            WHERE channel=?
        """, (channel,))
        conn.commit()
        return s["sent_today"] + 1


def reset_channel_daily_counter(channel: str = None):
    """Cron 00:00 — explicit reset. If channel=None, resets all."""
    from datetime import date
    today = date.today().isoformat()
    with _conn() as conn:
        if channel:
            conn.execute(
                "UPDATE outreach_channel_state SET sent_today=0, daily_reset_at=?, "
                "updated_at=datetime('now') WHERE channel=?",
                (today, channel)
            )
        else:
            conn.execute(
                "UPDATE outreach_channel_state SET sent_today=0, daily_reset_at=?, "
                "updated_at=datetime('now')",
                (today,)
            )
        conn.commit()


def channel_quota_remaining(channel: str) -> int:
    s = get_channel_state(channel)
    if not s:
        return 0
    return max(0, s["daily_limit"] - s["sent_today"])


# ════════════════════════════════════════════════════════════════════
# Growth Engine helpers — content_posts, funnel_events, audience_metrics
# ════════════════════════════════════════════════════════════════════

def insert_content_post(post: dict) -> int:
    """Saves new generated post σε pending status. Returns id."""
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO content_posts
                (platform, language, post_type, title, body, hashtags,
                 cta_url, media_url, target_region, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            post.get("platform", "linkedin"),
            post.get("language", "gr"),
            post.get("post_type", "text"),
            post.get("title", ""),
            post["body"],
            post.get("hashtags", ""),
            post.get("cta_url", ""),
            post.get("media_url", ""),
            post.get("target_region", "global"),
            post.get("status", "pending"),
            post.get("notes", ""),
        ))
        conn.commit()
        return cur.lastrowid


def list_pending_posts(platform: str = None, limit: int = 50) -> list:
    """Posts που περιμένουν approval."""
    with _conn() as conn:
        if platform:
            rows = conn.execute(
                "SELECT * FROM content_posts WHERE status='pending' AND platform=? "
                "ORDER BY generated_at DESC LIMIT ?", (platform, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM content_posts WHERE status='pending' "
                "ORDER BY generated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def list_approved_posts(limit: int = 20) -> list:
    """Posts approved αλλά not yet published — ready για auto-publisher."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM content_posts WHERE status='approved' "
            "ORDER BY approved_at ASC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_post_status(post_id: int, status: str, **fields) -> None:
    """Updates status + optional fields (buffer_id, published_url, etc)."""
    sets = ["status=?"]
    vals = [status]
    if status == "approved":
        sets.append("approved_at=datetime('now')")
        if "approved_by" in fields:
            sets.append("approved_by=?")
            vals.append(fields["approved_by"])
    if status == "published":
        sets.append("published_at=datetime('now')")
    for k in ("buffer_id", "published_url", "notes", "body", "hashtags"):
        if k in fields:
            sets.append(f"{k}=?")
            vals.append(fields[k])
    vals.append(post_id)
    with _conn() as conn:
        conn.execute(f"UPDATE content_posts SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()


def get_post(post_id: int) -> dict:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM content_posts WHERE id=?", (post_id,)).fetchone()
        return dict(row) if row else None


def list_recent_posts(limit: int = 20) -> list:
    """Πρόσφατα published posts για voice training context."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM content_posts WHERE status='published' "
            "ORDER BY published_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def log_funnel_event(event_type: str, source: str, **kwargs) -> None:
    """Log conversion funnel event."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO funnel_events
                (event_type, source, campaign, lead_email, lead_country, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            event_type, source,
            kwargs.get("campaign", ""),
            kwargs.get("lead_email", ""),
            kwargs.get("lead_country", ""),
            json.dumps(kwargs.get("metadata", {})),
        ))
        conn.commit()


def funnel_stats(days: int = 7) -> dict:
    """Returns aggregated funnel stats για dashboard."""
    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT event_type, source, COUNT(*) as cnt
            FROM funnel_events
            WHERE created_at >= datetime('now', '-{days} days')
            GROUP BY event_type, source
        """).fetchall()
        result = {}
        for r in rows:
            result.setdefault(r["source"], {})[r["event_type"]] = r["cnt"]
        return result


def upsert_audience_metric(platform: str, metric_date: str, **metrics) -> None:
    """Save daily snapshot per platform."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO audience_metrics
                (platform, metric_date, followers, reach, engagement,
                 profile_visits, website_clicks)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, metric_date) DO UPDATE SET
                followers=excluded.followers,
                reach=excluded.reach,
                engagement=excluded.engagement,
                profile_visits=excluded.profile_visits,
                website_clicks=excluded.website_clicks
        """, (
            platform, metric_date,
            metrics.get("followers", 0),
            metrics.get("reach", 0),
            metrics.get("engagement", 0),
            metrics.get("profile_visits", 0),
            metrics.get("website_clicks", 0),
        ))
        conn.commit()


def get_audience_growth(platform: str = None, days: int = 30) -> list:
    """Daily series για growth chart."""
    with _conn() as conn:
        if platform:
            rows = conn.execute(f"""
                SELECT * FROM audience_metrics
                WHERE platform=? AND metric_date >= date('now', '-{days} days')
                ORDER BY metric_date ASC
            """, (platform,)).fetchall()
        else:
            rows = conn.execute(f"""
                SELECT metric_date, SUM(followers) as followers,
                       SUM(reach) as reach, SUM(engagement) as engagement
                FROM audience_metrics
                WHERE metric_date >= date('now', '-{days} days')
                GROUP BY metric_date ORDER BY metric_date ASC
            """).fetchall()
        return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════
# Storefront helpers
# ════════════════════════════════════════════════════════════════════

def insert_store_order(order: dict) -> int:
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO store_orders
                (product_slug, customer_email, customer_name, customer_country,
                 customer_language, amount_cents, currency, stripe_session_id,
                 status, custom_input, referrer)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order["product_slug"],
            order["customer_email"],
            order.get("customer_name", ""),
            order.get("customer_country", ""),
            order.get("customer_language", "gr"),
            order["amount_cents"],
            order.get("currency", "eur"),
            order.get("stripe_session_id"),
            order.get("status", "pending"),
            json.dumps(order.get("custom_input", {}), ensure_ascii=False),
            order.get("referrer", ""),
        ))
        conn.commit()
        return cur.lastrowid


def get_order_by_session(session_id: str) -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM store_orders WHERE stripe_session_id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None


def get_order(order_id: int) -> dict:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM store_orders WHERE id=?", (order_id,)).fetchone()
        return dict(row) if row else None


def update_order_status(order_id: int, status: str, **fields) -> None:
    sets = ["status=?"]
    vals = [status]
    if status == "paid":
        sets.append("paid_at=datetime('now')")
        if "stripe_payment_intent" in fields:
            sets.append("stripe_payment_intent=?")
            vals.append(fields["stripe_payment_intent"])
    if status == "delivered":
        sets.append("delivered_at=datetime('now')")
    if "last_error" in fields:
        sets.append("last_error=?")
        vals.append(fields["last_error"])
    if "delivery_attempts" in fields:
        sets.append("delivery_attempts=?")
        vals.append(fields["delivery_attempts"])
    vals.append(order_id)
    with _conn() as conn:
        conn.execute(f"UPDATE store_orders SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()


def list_orders(status: str = None, limit: int = 100) -> list:
    with _conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM store_orders WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM store_orders ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def list_paid_undelivered_orders(limit: int = 20) -> list:
    """Orders που πληρώθηκαν αλλά δεν έχουν deliver-θεί ακόμα."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM store_orders
            WHERE status IN ('paid', 'delivering')
              AND delivery_attempts < 5
            ORDER BY paid_at ASC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def insert_delivery(order_id: int, content: str, **kwargs) -> int:
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO store_deliveries
                (order_id, content, content_type, tokens_used, model_used)
            VALUES (?, ?, ?, ?, ?)
        """, (
            order_id, content,
            kwargs.get("content_type", "markdown"),
            kwargs.get("tokens_used", 0),
            kwargs.get("model_used", ""),
        ))
        conn.commit()
        return cur.lastrowid


def get_delivery(order_id: int) -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM store_deliveries WHERE order_id=? ORDER BY generated_at DESC LIMIT 1",
            (order_id,)
        ).fetchone()
        return dict(row) if row else None


def store_revenue_stats(days: int = 30) -> dict:
    with _conn() as conn:
        # Total revenue
        rev = conn.execute(f"""
            SELECT COALESCE(SUM(amount_cents), 0) as total_cents,
                   COUNT(*) as order_count
            FROM store_orders
            WHERE status IN ('paid', 'delivering', 'delivered')
              AND paid_at >= datetime('now', '-{days} days')
        """).fetchone()
        # By product
        by_product = conn.execute(f"""
            SELECT product_slug, COUNT(*) as cnt, SUM(amount_cents) as total
            FROM store_orders
            WHERE status IN ('paid', 'delivering', 'delivered')
              AND paid_at >= datetime('now', '-{days} days')
            GROUP BY product_slug ORDER BY total DESC
        """).fetchall()
        return {
            "total_revenue_eur": (rev["total_cents"] or 0) / 100,
            "order_count": rev["order_count"] or 0,
            "by_product": [dict(r) for r in by_product],
        }


# ════════════════════════════════════════════════════════════════════
#  CUSTOMER MEMORY (AI θυμάται κάθε πελάτη — wave: AI memory layer)
# ════════════════════════════════════════════════════════════════════

def get_customer_memory(slug: str, identifier: str) -> dict | None:
    """Returns memory record για ένα customer (by phone or email)."""
    if not identifier:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM customer_memory WHERE slug=? AND identifier=?",
            (slug, identifier)
        ).fetchone()
    return dict(row) if row else None


def upsert_customer_memory(slug: str, identifier: str, **fields) -> int:
    """Upsert memory fields. Returns row id."""
    if not slug or not identifier:
        return 0
    allowed = {
        "identifier_type", "display_name", "personal_notes", "family_info",
        "preferred_time", "preferred_staff", "preferred_service",
        "last_topics", "conversation_summary",
    }
    fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
    with _conn() as conn:
        existing = conn.execute(
            "SELECT id, interaction_count FROM customer_memory WHERE slug=? AND identifier=?",
            (slug, identifier)
        ).fetchone()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if existing:
            sets, vals = [], []
            for k, v in fields.items():
                sets.append(f"{k}=?")
                vals.append(v)
            sets.append("interaction_count=interaction_count+1")
            sets.append("last_seen=?"); vals.append(now)
            sets.append("updated=?");   vals.append(now)
            vals += [slug, identifier]
            conn.execute(
                f"UPDATE customer_memory SET {', '.join(sets)} WHERE slug=? AND identifier=?",
                vals
            )
            conn.commit()
            return existing["id"]
        else:
            cols = ["slug", "identifier", "interaction_count", "last_seen"] + list(fields.keys())
            placeholders = ",".join(["?"] * len(cols))
            vals = [slug, identifier, 1, now] + list(fields.values())
            cur = conn.execute(
                f"INSERT INTO customer_memory ({','.join(cols)}) VALUES ({placeholders})",
                vals
            )
            conn.commit()
            return cur.lastrowid


def delete_customer_memory(slug: str, identifier: str) -> bool:
    """GDPR right-to-delete: removes all memory for this customer."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM customer_memory WHERE slug=? AND identifier=?",
            (slug, identifier)
        )
        conn.commit()
        return cur.rowcount > 0


def export_customer_data(slug: str, identifier: str) -> dict:
    """GDPR right-to-access: returns ALL data we have on this customer."""
    out = {"slug": slug, "identifier": identifier, "exported_at": datetime.now().isoformat()}
    with _conn() as conn:
        # Memory
        mem = conn.execute(
            "SELECT * FROM customer_memory WHERE slug=? AND identifier=?",
            (slug, identifier)
        ).fetchone()
        out["memory"] = dict(mem) if mem else None
        # Client profile (by phone)
        prof = conn.execute(
            "SELECT * FROM client_profiles WHERE slug=? AND phone=?",
            (slug, identifier)
        ).fetchone()
        out["client_profile"] = dict(prof) if prof else None
        # Bookings (by phone or email)
        bookings = conn.execute(
            "SELECT * FROM bookings WHERE slug=? AND (phone=? OR email=?)",
            (slug, identifier, identifier)
        ).fetchall()
        out["bookings"] = [dict(b) for b in bookings]
        # Consent logs
        consents = conn.execute(
            "SELECT * FROM consent_logs WHERE identifier=?",
            (identifier,)
        ).fetchall()
        out["consent_logs"] = [dict(c) for c in consents]
        # Audit logs
        audits = conn.execute(
            "SELECT * FROM audit_logs WHERE customer_id=?",
            (identifier,)
        ).fetchall()
        out["audit_logs"] = [dict(a) for a in audits]
    return out


def delete_all_customer_data(slug: str, identifier: str) -> dict:
    """GDPR Art.17 erasure — TWO CLOCKS (legally correct):
      • marketing/booking/contact metadata → deleted now.
      • clinical record (διαγνώσεις/notes/invoices) → NOT deleted: statutory 10y
        retention (ΠΔ 84/2001, GDPR Art.17(3)(b) legal-obligation exception). It is
        placed under legal hold and logged for a scheduled purge at expiry.
    Deleting the clinical record on request would itself be unlawful."""
    deleted = {}
    with _conn() as conn:
        for table, where, params in [
            ("customer_memory", "slug=? AND identifier=?", (slug, identifier)),
            ("client_profiles", "slug=? AND phone=?", (slug, identifier)),
            ("bookings",        "slug=? AND (phone=? OR email=?)", (slug, identifier, identifier)),
            ("voice_calls",     "business_slug=? AND (from_number=? OR to_number=?)", (slug, identifier, identifier)),
            ("consent_logs",    "slug=? AND identifier=?", (slug, identifier)),
            ("outbound_jobs",   "slug=? AND (customer_phone=? OR customer_email=?)", (slug, identifier, identifier)),
        ]:
            try:
                cur = conn.execute(f"DELETE FROM {table} WHERE {where}", params)
                deleted[table] = cur.rowcount
            except Exception as exc:  # column/table differences must not abort the erasure
                deleted[table] = f"skip:{type(exc).__name__}"
        # Clinical record: legal hold (do NOT delete) + log for scheduled purge at expiry.
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS erasure_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, identifier TEXT,
                requested_at TEXT DEFAULT (datetime('now')),
                clinical_purge_due TEXT, status TEXT DEFAULT 'metadata_erased_clinical_held')""")
            conn.execute(
                "INSERT INTO erasure_requests(slug, identifier, clinical_purge_due) "
                "VALUES (?, ?, date('now','+10 years'))", (slug, identifier))
        except Exception:
            pass
        conn.commit()
    deleted["clinical_record"] = "retained_under_legal_hold_10y (ΠΔ 84/2001)"
    return deleted


# ════════════════════════════════════════════════════════════════════
#  OUTBOUND JOBS (Feature #3: Outbound Proactive AI — 2026-05-28)
# ════════════════════════════════════════════════════════════════════

VALID_JOB_TYPES = ("pre_arrival_upsell", "no_show_reminder", "review_request", "win_back",
                   "no_show_recovery", "owner_briefing")
VALID_JOB_CHANNELS = ("voice", "sms", "email")


def enqueue_outbound_job(slug: str, job_type: str, customer_phone: str,
                         scheduled_at: str, channel: str = "voice",
                         customer_name: str = "", customer_email: str = "",
                         booking_id: Optional[int] = None,
                         payload: Optional[dict] = None,
                         dedup: bool = True) -> Optional[int]:
    """Queue a single outbound job. Returns row id, or None if duplicate (when dedup=True)."""
    if job_type not in VALID_JOB_TYPES:
        raise ValueError(f"Invalid job_type '{job_type}'. Allowed: {VALID_JOB_TYPES}")
    if channel not in VALID_JOB_CHANNELS:
        raise ValueError(f"Invalid channel '{channel}'. Allowed: {VALID_JOB_CHANNELS}")
    if not customer_phone:
        return None

    with _conn() as conn:
        if dedup and booking_id:
            existing = conn.execute(
                "SELECT id FROM outbound_jobs "
                "WHERE booking_id=? AND job_type=? AND status IN ('pending','done')",
                (booking_id, job_type)
            ).fetchone()
            if existing:
                return None

        cur = conn.execute("""
            INSERT INTO outbound_jobs
                (slug, job_type, channel, customer_phone, customer_name,
                 customer_email, booking_id, payload_json, scheduled_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            slug, job_type, channel, customer_phone, customer_name or "",
            customer_email or "", booking_id,
            json.dumps(payload or {}, ensure_ascii=False), scheduled_at,
        ))
        conn.commit()
        return cur.lastrowid


def get_due_outbound_jobs(limit: int = 20) -> list:
    """Returns pending jobs whose scheduled_at <= now."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM outbound_jobs
            WHERE status='pending' AND scheduled_at <= ?
            ORDER BY scheduled_at ASC
            LIMIT ?
        """, (now, limit)).fetchall()
    return [dict(r) for r in rows]


def mark_outbound_job(job_id: int, status: str, result: str = "") -> bool:
    """status in: done, failed, cancelled. Increments attempts."""
    if status not in ("done", "failed", "cancelled", "pending"):
        return False
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        cur = conn.execute("""
            UPDATE outbound_jobs
            SET status=?, result=?, attempts=attempts+1, processed_at=?
            WHERE id=?
        """, (status, (result or "")[:500], now, job_id))
        conn.commit()
        return cur.rowcount > 0


def list_outbound_jobs(slug: str = None, status: str = None,
                       limit: int = 100) -> list:
    """Admin/UI listing."""
    where, params = [], []
    if slug:   where.append("slug=?");   params.append(slug)
    if status: where.append("status=?"); params.append(status)
    sql = "SELECT * FROM outbound_jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY scheduled_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def retry_outbound_job(job_id: int, delay_minutes: int = 30) -> bool:
    """P5 retry rail: transient failure → ξαναπρογραμμάτισε +N λεπτά, μένει pending."""
    when = (datetime.now() + timedelta(minutes=delay_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        cur = conn.execute("""
            UPDATE outbound_jobs
            SET scheduled_at=?, attempts=attempts+1, result=?
            WHERE id=? AND status='pending'
        """, (when, f"retry_scheduled_{when}", job_id))
        conn.commit()
        return cur.rowcount > 0


def count_outbound_done_today(slug: str) -> int:
    """P5 cost rail: πόσα outbound jobs εκτελέστηκαν σήμερα για ένα business."""
    with _conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) FROM outbound_jobs
            WHERE slug=? AND status='done' AND date(processed_at)=date('now','localtime')
        """, (slug,)).fetchone()
    return int(row[0]) if row else 0


def cancel_outbound_jobs_for_booking(booking_id: int) -> int:
    """When a booking is cancelled, cancel all pending outbound jobs for it."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE outbound_jobs SET status='cancelled' "
            "WHERE booking_id=? AND status='pending'", (booking_id,)
        )
        conn.commit()
        return cur.rowcount


# ════════════════════════════════════════════════════════════════════
#  AUDIT LOGS (EU AI Act compliance)
# ════════════════════════════════════════════════════════════════════

def log_audit(action: str, slug: str = None, model: str = None,
              customer_id: str = None, ip_hash: str = None,
              actor_type: str = "system", actor_id: str = None,
              details: str = "") -> int:
    """Append a TAMPER-EVIDENT audit record: row_hash = SHA256(prev_hash + payload),
    so any later edit/insert/delete breaks the chain (Art.5(2) accountability,
    EU AI Act Art.12). ⚠️ Never put plaintext PHI in `details` — use record id +
    field name, not the diagnosis."""
    import hashlib
    import json
    from datetime import datetime, timezone
    with _conn() as conn:
        prev = conn.execute(
            "SELECT row_hash FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = (prev[0] if prev and prev[0] else "0" * 64)
        created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        payload = json.dumps({
            "slug": slug, "action": action, "actor_type": actor_type, "actor_id": actor_id,
            "model": model, "customer_id": customer_id, "ip_hash": ip_hash,
            "details": details, "created": created,
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        row_hash = hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()
        cur = conn.execute(
            """INSERT INTO audit_logs (slug, action, actor_type, actor_id, model,
               customer_id, ip_hash, details, created, prev_hash, row_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (slug, action, actor_type, actor_id, model, customer_id, ip_hash,
             details, created, prev_hash, row_hash)
        )
        conn.commit()
        return cur.lastrowid


def verify_audit_chain(slug: str = None) -> dict:
    """Re-derive the whole audit chain; detect any tampering (edit/insert/delete)."""
    import hashlib
    import json
    with _conn() as conn:
        if slug:
            rows = conn.execute(
                "SELECT * FROM audit_logs WHERE slug=? ORDER BY id ASC", (slug,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit_logs ORDER BY id ASC").fetchall()
    prev_hash = "0" * 64
    for r in rows:
        r = dict(r)
        if r.get("row_hash") is None:
            continue  # legacy pre-chain rows
        payload = json.dumps({
            "slug": r["slug"], "action": r["action"], "actor_type": r["actor_type"],
            "actor_id": r["actor_id"], "model": r["model"], "customer_id": r["customer_id"],
            "ip_hash": r["ip_hash"], "details": r["details"], "created": r["created"],
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        expected = hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()
        if expected != r["row_hash"]:
            return {"intact": False, "broken_at_id": r["id"], "verified": True}
        prev_hash = r["row_hash"]
    return {"intact": True, "rows": len(rows)}


def list_audit_logs(slug: str = None, customer_id: str = None,
                    limit: int = 100) -> list:
    with _conn() as conn:
        if slug and customer_id:
            rows = conn.execute(
                "SELECT * FROM audit_logs WHERE slug=? AND customer_id=? ORDER BY created DESC LIMIT ?",
                (slug, customer_id, limit)
            ).fetchall()
        elif slug:
            rows = conn.execute(
                "SELECT * FROM audit_logs WHERE slug=? ORDER BY created DESC LIMIT ?",
                (slug, limit)
            ).fetchall()
        elif customer_id:
            rows = conn.execute(
                "SELECT * FROM audit_logs WHERE customer_id=? ORDER BY created DESC LIMIT ?",
                (customer_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_logs ORDER BY created DESC LIMIT ?",
                (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════
#  CONSENT LOGS (GDPR + EU AI Act)
# ════════════════════════════════════════════════════════════════════

def log_consent(consent_type: str, slug: str = None, identifier: str = None,
                identifier_type: str = "session", consent_version: str = "1.0",
                accepted: bool = True, ip_hash: str = None,
                user_agent: str = "") -> int:
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO consent_logs (slug, identifier, identifier_type,
               consent_type, consent_version, accepted, ip_hash, user_agent)
               VALUES (?,?,?,?,?,?,?,?)""",
            (slug, identifier, identifier_type, consent_type, consent_version,
             1 if accepted else 0, ip_hash, user_agent[:300])
        )
        conn.commit()
        return cur.lastrowid


def get_consent_status(identifier: str, consent_type: str = "ai_disclosure") -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            """SELECT * FROM consent_logs WHERE identifier=? AND consent_type=?
               ORDER BY created DESC LIMIT 1""",
            (identifier, consent_type)
        ).fetchone()
    return dict(row) if row else None
