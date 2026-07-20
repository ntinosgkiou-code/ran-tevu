"""
test_backup.py — αποδεικνύει consistent backups + rotation + καμία απώλεια δεδομένων.
Run: python core/test_backup.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import os
import sqlite3
import tempfile

from store import KVStore
import backup as bk


def test_backup_preserves_data():
    d = tempfile.mkdtemp()
    src = os.path.join(d, "clinic.db")
    kv = KVStore(src)
    kv.put("patient:c1", "p1", {"id": "p1", "name": "Δοκιμή", "amka": "11111111111"})
    kv.close()
    dest = bk.backup_db(src, backup_dir=os.path.join(d, "backups"))
    assert dest and os.path.exists(dest), "το backup δεν δημιουργήθηκε"
    # το αντίγραφο περιέχει την ίδια row (και είναι ξεχωριστό αρχείο)
    con = sqlite3.connect(dest)
    n = con.execute("SELECT COUNT(*) FROM kv").fetchone()[0]
    con.close()
    assert n == 1, f"το backup δεν έχει τα δεδομένα (rows={n})"
    print("✅ backup = συνεπές, ξεχωριστό αρχείο, με όλα τα δεδομένα")


def test_rotation_keeps_only_n():
    # φτιάξε 6 backups με ΔΙΑΦΟΡΕΤΙΚΑ ονόματα (όπως σε διαφορετικές μέρες) και έλεγξε ότι το
    # _rotate κρατά ΑΚΡΙΒΩΣ τα 3 πιο πρόσφατα και ΣΒΗΝΕΙ τα υπόλοιπα.
    d = tempfile.mkdtemp()
    bdir = os.path.join(d, "backups"); os.makedirs(bdir)
    for stamp in ("20260101-090000", "20260102-090000", "20260103-090000",
                  "20260104-090000", "20260105-090000", "20260106-090000"):
        open(os.path.join(bdir, f"clinic-{stamp}.db"), "w").close()
    bk._rotate(bdir, "clinic", keep=3)
    files = sorted(f for f in os.listdir(bdir) if f.endswith(".db"))
    assert files == ["clinic-20260104-090000.db", "clinic-20260105-090000.db",
                     "clinic-20260106-090000.db"], files
    print(f"✅ rotation: κρατήθηκαν τα 3 πιο πρόσφατα, σβήστηκαν τα 3 παλιότερα")


def test_memory_db_is_noop():
    assert bk.backup_db(":memory:") is None
    assert bk.backup_db("") is None
    print("✅ :memory:/κενό → no-op (κανένα ψεύτικο backup)")


def main():
    test_backup_preserves_data()
    test_rotation_keeps_only_n()
    test_memory_db_is_noop()
    print("\n💾 BACKUPS: ALL TESTS PASSED — consistent, rotated, δεν χάνεται το βιβλίο ασθενών")


if __name__ == "__main__":
    main()
