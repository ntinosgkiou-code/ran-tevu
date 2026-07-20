"""
Memory service — AI θυμάται κάθε customer.

Wave: AI memory layer (peak 2027-2028).
Used by app.py chat endpoint to:
  1. Fetch existing memory before building system prompt
  2. After conversation, extract personal facts + summary, write back

Storage: customer_memory table (database.py)
Identifier: phone number (preferred) or email
"""

import json
import os
import re
import threading
from typing import Optional

import anthropic

from database import (
    get_customer_memory,
    upsert_customer_memory,
    log_audit,
)

_claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


# ─────────────────────────────────────────────────────────────────────
# Identifier extraction (phone preferred over email)
# ─────────────────────────────────────────────────────────────────────

_PHONE_RE = re.compile(r"(?:\+?\d[\d\s\-]{8,}\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def extract_identifier(messages: list) -> tuple[Optional[str], str]:
    """
    Walks USER messages, returns (identifier, type) — phone wins over email.
    Returns (None, "") if nothing found.
    """
    phone = None
    email = None
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        if not isinstance(content, str):
            continue
        if not phone:
            ph = _PHONE_RE.search(content)
            if ph:
                phone = re.sub(r"[\s\-]", "", ph.group(0))
                if len(phone) >= 10:
                    pass
                else:
                    phone = None
        if not email:
            em = _EMAIL_RE.search(content)
            if em:
                email = em.group(0).lower().strip()
    if phone:
        return phone, "phone"
    if email:
        return email, "email"
    return None, ""


# ─────────────────────────────────────────────────────────────────────
# Memory → system prompt section
# ─────────────────────────────────────────────────────────────────────

def render_memory_section(memory: dict, lang: str = "el") -> str:
    """Build prompt text from memory dict. Returns '' if no useful memory."""
    if not memory:
        return ""

    name = memory.get("display_name") or ""
    notes = memory.get("personal_notes") or ""
    family = memory.get("family_info") or ""
    pref_time = memory.get("preferred_time") or ""
    pref_staff = memory.get("preferred_staff") or ""
    pref_service = memory.get("preferred_service") or ""
    last_topics = memory.get("last_topics") or ""
    summary = memory.get("conversation_summary") or ""
    interactions = memory.get("interaction_count", 0) or 0
    last_seen = memory.get("last_seen") or ""

    has_content = any([name, notes, family, pref_time, pref_staff, pref_service,
                       last_topics, summary])
    if not has_content:
        return ""

    if lang == "en":
        lines = ["━━━ RETURNING CUSTOMER MEMORY ━━━"]
        if name:         lines.append(f"Name: {name}")
        if interactions: lines.append(f"Past interactions: {interactions} (last: {last_seen})")
        if pref_service: lines.append(f"Usual service: {pref_service}")
        if pref_staff:   lines.append(f"Prefers: {pref_staff}")
        if pref_time:    lines.append(f"Preferred time slots: {pref_time}")
        if family:       lines.append(f"Family/personal: {family}")
        if notes:        lines.append(f"Personal notes: {notes}")
        if last_topics:  lines.append(f"Last topics discussed: {last_topics}")
        if summary:      lines.append(f"Last conversation summary: {summary}")
        lines.append("")
        lines.append("→ Greet by first name naturally.")
        lines.append("→ Reference past visit ONLY if relevant (don't force it).")
        lines.append("→ Acknowledge personal facts you remember (e.g., 'How is your daughter?')")
        lines.append("→ Suggest their usual service/time if they don't specify.")
        lines.append("→ Be warm, not robotic. Sound like someone who knows them.")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    else:
        lines = ["━━━ ΘΥΜΑΜΑΙ ΤΟΝ ΠΕΛΑΤΗ (MEMORY) ━━━"]
        if name:         lines.append(f"Όνομα: {name}")
        if interactions: lines.append(f"Προηγούμενες επαφές: {interactions} (τελευταία: {last_seen})")
        if pref_service: lines.append(f"Συνηθισμένη υπηρεσία: {pref_service}")
        if pref_staff:   lines.append(f"Προτιμά: {pref_staff}")
        if pref_time:    lines.append(f"Προτιμώμενες ώρες: {pref_time}")
        if family:       lines.append(f"Οικογένεια/προσωπικά: {family}")
        if notes:        lines.append(f"Σημειώσεις: {notes}")
        if last_topics:  lines.append(f"Τελευταία θέματα: {last_topics}")
        if summary:      lines.append(f"Σύνοψη τελευταίας κουβέντας: {summary}")
        lines.append("")
        lines.append("→ Χαιρέτα τον/την με το ΜΙΚΡΟ ΟΝΟΜΑ φυσικά.")
        lines.append("→ Αναφέρσου σε προηγούμενη επίσκεψη ΜΟΝΟ αν ταιριάζει.")
        lines.append("→ Αν θυμάσαι κάτι προσωπικό (π.χ. παιδί που ήταν άρρωστο), ρώτα για αυτό.")
        lines.append("→ Πρότεινε την συνηθισμένη υπηρεσία/ώρα αν δεν διευκρινίσει.")
        lines.append("→ Μίλα ΖΕΣΤΑ, ΟΧΙ ρομποτικά. Σαν κάποια/ος που τον/την ξέρει.")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────
# Fact extraction + summarization (Claude Haiku — γρήγορο + φθηνό)
# ─────────────────────────────────────────────────────────────────────

_FACT_PROMPT = """Είσαι ένα memory extraction system. Διαβάζεις μια συνομιλία μεταξύ AI bot και πελάτη.
Βγάζεις ΜΟΝΟ διαρκείς, χρήσιμες πληροφορίες (όχι one-off).

Επιστρέφεις ΚΑΘΑΡΟ JSON (χωρίς markdown), με ΑΥΤΑ τα keys (όλα optional, παράλειψε άδεια):
{
  "display_name": "μικρό όνομα, π.χ. Μαρία",
  "personal_notes": "ένα-δύο πράγματα προσωπικά που είπε (π.χ. πρόβλημα ευαίσθητο δέρμα)",
  "family_info": "οικογενειακό context (π.χ. έχει 2 παιδιά, ένα Στράτος)",
  "preferred_time": "ώρες/μέρες που προτιμά",
  "preferred_staff": "ποιον/ποια ζητάει συνήθως",
  "preferred_service": "τι υπηρεσία κάνει συνήθως",
  "last_topics": "1-2 keywords από αυτή τη συνομιλία (π.χ. ραντεβού, καθαρισμός)",
  "conversation_summary": "1 σύντομη πρόταση τι συνέβη"
}

ΚΑΝΟΝΕΣ:
- Αν δεν υπάρχει πληροφορία για ένα key, ΠΑΡΑΛΕΙΨΕ το (μη βάλεις κενό string).
- ΜΗ φαντάζεσαι. Μόνο όσα ειπώθηκαν.
- Σύντομα. Max 100 chars per field.
- ΜΟΝΟ JSON, τίποτα άλλο.

ΣΥΝΟΜΙΛΙΑ:
{conversation}

JSON:"""


def _format_conversation(messages: list, max_msgs: int = 20) -> str:
    """Convert messages list to readable transcript for the extractor."""
    out = []
    for m in messages[-max_msgs:]:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif isinstance(b, str):
                    parts.append(b)
            content = " ".join(parts)
        if not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        prefix = "Πελάτης" if role == "user" else "Bot"
        out.append(f"{prefix}: {content}")
    return "\n".join(out)


def extract_facts_from_conversation(messages: list) -> dict:
    """
    Sends conversation to Claude Haiku, gets back structured facts.
    Returns {} on failure.
    """
    if not messages or len(messages) < 2:
        return {}
    transcript = _format_conversation(messages)
    if not transcript or len(transcript) < 50:
        return {}
    try:
        resp = _claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": _FACT_PROMPT.replace("{conversation}", transcript[:4000])
            }],
        )
        text = ""
        for b in resp.content:
            if hasattr(b, "text"):
                text += b.text
        text = text.strip()
        # Strip markdown fences if model added them
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return {k: str(v)[:300] for k, v in data.items() if v}
        except json.JSONDecodeError:
            pass
    except Exception as e:
        print(f"[memory_service] extract_facts failed: {e}", flush=True)
    return {}


# ─────────────────────────────────────────────────────────────────────
# Public API: store conversation memory (called after each chat)
# ─────────────────────────────────────────────────────────────────────

def store_conversation_memory(slug: str, messages: list, identifier: str = None,
                              identifier_type: str = "phone", async_mode: bool = True):
    """
    Top-level: extract facts from conversation, upsert into customer_memory.
    If identifier not given, tries to extract from messages.
    async_mode=True runs in background thread (doesn't block chat response).
    """
    if not slug:
        return
    if not identifier:
        identifier, identifier_type = extract_identifier(messages)
    if not identifier:
        return  # No way to remember this customer

    def _work():
        try:
            facts = extract_facts_from_conversation(messages)
            if not facts:
                # Still bump interaction_count + last_seen
                upsert_customer_memory(slug, identifier, identifier_type=identifier_type)
                return
            facts["identifier_type"] = identifier_type
            upsert_customer_memory(slug, identifier, **facts)
            log_audit(
                action="memory_write",
                slug=slug,
                model="claude-haiku-4-5",
                customer_id=identifier,
                details=f"keys={list(facts.keys())}"
            )
        except Exception as e:
            print(f"[memory_service] store failed: {e}", flush=True)

    if async_mode:
        t = threading.Thread(target=_work, daemon=True)
        t.start()
    else:
        _work()


def fetch_memory_for_prompt(slug: str, messages: list = None,
                            identifier: str = None) -> tuple[Optional[dict], Optional[str]]:
    """
    Returns (memory_dict, identifier) — fetches from DB.
    Called BEFORE building system prompt.
    """
    if not identifier and messages:
        identifier, _ = extract_identifier(messages)
    if not identifier:
        return None, None
    mem = get_customer_memory(slug, identifier)
    return mem, identifier
