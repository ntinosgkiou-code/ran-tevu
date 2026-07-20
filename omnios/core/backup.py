"""
backup.py — αυτόματα, συνεπή backups του συστήματος-αρχείου (μην χαθεί ποτέ το βιβλίο ασθενών).
==============================================================================================
Ένα ιατρείο ΔΕΝ μπορεί να ρισκάρει το SQLite του σε ένα μόνο αρχείο: corruption ή βλάβη δίσκου
= χαμένα ραντεβού/ασθενείς/τιμολόγια. Αυτό παίρνει **online** αντίγραφο (SQLite backup API) που
είναι συνεπές ΑΚΟΜΑ κι όταν το DB γράφεται εκείνη τη στιγμή (WAL-safe), με rotation (κρατάει τα
τελευταία N). Τα backups κρυπτογραφούνται ΗΔΗ στο επίπεδο των values (crypto_at_rest) — άρα ένα
αντίγραφο δεν εκθέτει PII περισσότερο απ' το πρωτότυπο.

Deterministic, stdlib-only. Tests: python core/test_backup.py
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Optional

import config

_KEEP_DEFAULT = int(os.environ.get("OMNIOS_BACKUP_KEEP", "14"))


def _real_db_paths() -> list[str]:
    """Distinct on-disk SQLite files που αξίζει να κρατηθούν (αγνοεί :memory:)."""
    candidates = [
        os.environ.get("OMNIOS_CLINIC_DB", ""),   # τα δεδομένα ασθενών — το πιο κρίσιμο
        config.DB_PATH,                            # brain/audit chain
        config.SAGA_DB,
    ]
    seen: list[str] = []
    for p in candidates:
        if p and p != ":memory:" and p not in seen and os.path.exists(p):
            seen.append(p)
    return seen


def backup_db(src_path: str, backup_dir: Optional[str] = None, keep: int = _KEEP_DEFAULT) -> Optional[str]:
    """Consistent αντίγραφο του src_path. Επιστρέφει το path του backup, ή None αν δεν εφαρμόζεται."""
    if not src_path or src_path == ":memory:" or not os.path.exists(src_path):
        return None
    backup_dir = backup_dir or os.environ.get("OMNIOS_BACKUP_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(src_path)), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src_path))[0]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(backup_dir, f"{base}-{stamp}.db")
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dest)
    try:
        with dst:                      # SQLite online backup — atomic & WAL-consistent
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    _rotate(backup_dir, base, keep)
    return dest


def _rotate(backup_dir: str, base: str, keep: int) -> None:
    """Κράτα τα πιο πρόσφατα `keep` backups αυτού του DB· σβήσε τα παλιότερα."""
    files = sorted(
        (f for f in os.listdir(backup_dir) if f.startswith(base + "-") and f.endswith(".db")),
        reverse=True)
    for stale in files[max(1, keep):]:
        try:
            os.remove(os.path.join(backup_dir, stale))
        except OSError:
            pass


def backup_all(keep: int = _KEEP_DEFAULT) -> list[str]:
    """Backup κάθε πραγματικού DB (clinic + brain/audit). Επιστρέφει τα paths που δημιουργήθηκαν."""
    out = []
    for p in _real_db_paths():
        try:
            d = backup_db(p, keep=keep)
            if d:
                out.append(d)
        except Exception as exc:   # ένα backup που αποτυγχάνει ΠΟΤΕ δεν ρίχνει το app
            print(f"[BACKUP] απέτυχε για {p}: {exc.__class__.__name__}")
    return out


def maybe_backup_all(state: dict, interval_s: int, keep: int = _KEEP_DEFAULT) -> list[str]:
    """Time-gated: τρέχει backup μόνο αν πέρασε το interval από το προηγούμενο (για το heartbeat)."""
    now = time.time()
    if now - state.get("last", 0.0) < interval_s:
        return []
    state["last"] = now
    return backup_all(keep=keep)
