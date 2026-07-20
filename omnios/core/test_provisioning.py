"""
test_provisioning.py — runtime clinic provisioning: admin-gated, isolated, survives restart.
Run: python core/test_provisioning.py
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import os
import tempfile

_fd, _dbp = tempfile.mkstemp(suffix=".db"); os.close(_fd); os.remove(_dbp)
os.environ["OMNIOS_CLINIC_DB"] = _dbp
os.environ["OMNIOS_ADMIN_KEY"] = "admin-secret-key-abcdef123456"

from fastapi.testclient import TestClient
import api
from api import app, _ADMIN_KEY, _REG, _TENANT_KEYS, _keys, _load_persisted_tenants

anon = TestClient(app)
NEW = {"slug": "neo-iatreio", "name": "Νέο Ορθοπεδικό", "specialty": "Ορθοπεδική", "default_value": 45.0}


def test_admin_gated():
    assert anon.post("/admin/clinics", json=NEW).status_code == 401          # no key
    assert anon.post("/admin/clinics", headers={"X-Admin-Key": "wrong"}, json=NEW).status_code == 401
    print("✅ provisioning admin-gated: 401 χωρίς/λάθος OMNIOS_ADMIN_KEY")


def test_create_and_isolated():
    r = anon.post("/admin/clinics", headers={"X-Admin-Key": _ADMIN_KEY}, json=NEW)
    assert r.status_code == 200, r.text
    key = r.json()["api_key"]
    assert key and key.startswith("sk_")
    # το key του νέου ιατρείου βλέπει ΤΟ ΔΙΚΟ του και μόνο
    kc = TestClient(app, headers={"X-API-Key": key})
    j = kc.get("/clinic").json()
    assert j["clinic"]["name"] == "Νέο Ορθοπεδικό", j
    # δεν βλέπει τους ασθενείς άλλου ιατρείου (κενό — μόλις στήθηκε)
    assert kc.get("/clinic/patients").json()["patients"] == []
    # duplicate → 409
    assert anon.post("/admin/clinics", headers={"X-Admin-Key": _ADMIN_KEY}, json=NEW).status_code == 409
    print("✅ create: 200 + pinned key, isolated, duplicate→409")
    return key


def test_survives_restart(key):
    # προσομοίωση restart: βγάλ' το από το live registry, μετά reload από persisted store
    _REG.pop("neo-iatreio", None); _TENANT_KEYS.pop("neo-iatreio", None); _keys.revoke(key)
    assert "neo-iatreio" not in _REG
    _load_persisted_tenants()                       # ό,τι τρέχει στο boot
    assert "neo-iatreio" in _REG, "το ιατρείο ΔΕΝ επανήλθε από το persisted registry!"
    kc = TestClient(app, headers={"X-API-Key": key})
    assert kc.get("/clinic").status_code == 200, "το key ΔΕΝ επέζησε του restart"
    assert kc.get("/clinic").json()["clinic"]["name"] == "Νέο Ορθοπεδικό"
    print("✅ επιβιώνει restart: tenant + key reload-άρουν από το persisted registry")


def main():
    test_admin_gated()
    key = test_create_and_isolated()
    test_survives_restart(key)
    print("\n🏥 RUNTIME PROVISIONING: ALL TESTS PASSED — νέο ιατρείο χωρίς κώδικα, isolated, persistent")
    try:
        os.remove(_dbp)
    except OSError:
        pass


if __name__ == "__main__":
    main()
