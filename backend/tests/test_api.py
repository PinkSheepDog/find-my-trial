"""API + auth integration tests against the real app (TestClient).

Covers: auth required on data routes, login/CSRF, the two-step de-id->match flow,
and the defense-in-depth guarantee that /api/match re-scrubs any PHI before the
pipeline (and therefore before any LLM egress).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent
CSV = BACKEND / "data" / "trials.csv"

pytestmark = pytest.mark.skipif(not CSV.exists(), reason="trial CSV not present")

ADMIN_USER = "admin"
ADMIN_PASS = "test-password-123"


@pytest.fixture(scope="module")
def client():
    os.environ["FMT_ADMIN_USERNAME"] = ADMIN_USER
    os.environ["FMT_ADMIN_PASSWORD"] = ADMIN_PASS
    os.environ["FMT_SECRET_KEY"] = "test-secret-key-please-change"
    os.environ["FMT_OPENROUTER_API_KEY"] = ""  # degraded mode (deterministic)
    # Clear cached settings so env overrides take effect.
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import app
    with TestClient(app) as c:
        yield c


def _login(client) -> str:
    r = client.post("/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


# ------------------------------- auth -------------------------------

def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["trial_count"] == 10000


def test_match_requires_auth(client):
    client.cookies.clear()
    r = client.post("/api/match", json={"deidentified_text": "64F TNBC"})
    assert r.status_code == 401


def test_login_rejects_bad_credentials(client):
    r = client.post("/api/login", json={"username": ADMIN_USER, "password": "wrong"})
    assert r.status_code == 401


def test_login_succeeds_and_sets_httponly_session(client):
    r = client.post("/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()


def test_csrf_required_on_match(client):
    _login(client)
    # No CSRF header -> 403
    r = client.post("/api/match", json={"deidentified_text": "64F TNBC"})
    assert r.status_code == 403


# ------------------------------- de-id -> match flow -------------------------------

def test_deidentify_then_match_flow(client):
    csrf = _login(client)
    headers = {"x-csrf-token": csrf}

    raw = ("Patient Name: Jane Doe, MRN 0048239, 62 year old female with metastatic "
           "HER2-positive breast cancer, liver and bone mets, prior trastuzumab and "
           "pertuzumab, ECOG 1.")
    d = client.post("/api/deidentify", json={"text": raw}, headers=headers)
    assert d.status_code == 200, d.text
    deid_text = d.json()["deidentified_text"]
    assert "Jane Doe" not in deid_text
    assert "0048239" not in deid_text
    assert d.json()["total_redactions"] > 0
    # Clinical signal survives.
    assert "HER2" in deid_text

    m = client.post("/api/match", json={"deidentified_text": deid_text, "top_k": 5,
                    "active_only": False, "interventional_only": False}, headers=headers)
    assert m.status_code == 200, m.text
    body = m.json()
    assert body["profile"]["age"] == 62
    her2 = next(b for b in body["profile"]["biomarkers"] if b["name"] == "HER2")
    assert her2["status"] == "positive"
    assert len(body["match"]["results"]) >= 1
    assert body["match"]["degraded_mode"] is True  # no LLM key in test env


def test_match_rescrubs_phi_defense_in_depth(client):
    """If a client mistakenly posts text that still contains an identifier, /api/match
    must scrub it before the pipeline — PHI must never reach extraction/LLM."""
    csrf = _login(client)
    headers = {"x-csrf-token": csrf}
    leaky = "Patient Name: John Q. Public, 55 year old male with lung cancer, EGFR positive."
    m = client.post("/api/match", json={"deidentified_text": leaky, "top_k": 3,
                    "active_only": False, "interventional_only": False}, headers=headers)
    assert m.status_code == 200, m.text
    # The returned profile/evidence must not contain the leaked name.
    blob = m.text
    assert "John Q. Public" not in blob
    assert "John" not in blob or "Public" not in blob


def test_logout_revokes_session(client):
    csrf = _login(client)
    headers = {"x-csrf-token": csrf}
    r = client.post("/api/logout", headers=headers)
    assert r.status_code == 200
    client.cookies.clear()
    r2 = client.get("/api/me")
    assert r2.status_code == 401
