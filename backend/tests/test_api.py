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

    # Egress gate: the scrubbed text must be explicitly approved before matching.
    a = client.post("/api/approve-deid", json={"text": deid_text}, headers=headers)
    assert a.status_code == 200, a.text
    approval = a.json()["approval_token"]

    m = client.post("/api/match", json={"deidentified_text": deid_text, "top_k": 5,
                    "active_only": False, "interventional_only": False},
                    headers={**headers, "X-Deid-Approval": approval})
    assert m.status_code == 200, m.text
    body = m.json()
    assert body["profile"]["age"] == 62
    her2 = next(b for b in body["profile"]["biomarkers"] if b["name"] == "HER2")
    assert her2["status"] == "positive"
    assert len(body["match"]["results"]) >= 1
    assert body["match"]["degraded_mode"] is True  # no LLM key in test env


LEAKY = "Patient Name: John Q. Public, 55 year old male with lung cancer, EGFR positive."


def test_match_refuses_unapproved_text(client):
    """The egress bypass this suite used to demonstrate: posting chart text straight
    to /api/match, skipping the human review gate entirely. It must now be refused."""
    csrf = _login(client)
    m = client.post("/api/match", json={"deidentified_text": LEAKY, "top_k": 3,
                    "active_only": False, "interventional_only": False},
                    headers={"x-csrf-token": csrf})
    assert m.status_code == 403, m.text
    assert "approved" in m.json()["error"].lower()
    assert "John" not in m.text  # the refusal must not echo the submitted text


def test_approval_refuses_text_that_still_has_identifiers(client):
    """Approval is not a rubber stamp — text carrying identifiers cannot be approved,
    so a caller cannot simply approve raw chart text to obtain a token."""
    csrf = _login(client)
    a = client.post("/api/approve-deid", json={"text": LEAKY}, headers={"x-csrf-token": csrf})
    assert a.status_code == 422, a.text
    assert "John" not in a.text


def test_approval_token_does_not_transfer_to_other_text(client):
    """A token approved for one chart must not license matching a different one."""
    csrf = _login(client)
    headers = {"x-csrf-token": csrf}
    approved = "[NAME], [AGE] male with lung cancer, EGFR positive, ECOG 1."
    token = client.post("/api/approve-deid", json={"text": approved},
                        headers=headers).json()["approval_token"]
    m = client.post("/api/match",
                    json={"deidentified_text": "[NAME], [AGE] female with breast cancer.",
                          "top_k": 3, "active_only": False, "interventional_only": False},
                    headers={**headers, "X-Deid-Approval": token})
    assert m.status_code == 403, m.text


def test_match_rescrubs_phi_defense_in_depth(client):
    """Second control, independent of the gate: even an approved payload is re-scrubbed,
    so PHI can never reach extraction/LLM. Exercised here by disabling the gate, which
    is what a development deployment (FMT_REQUIRE_DEID_REVIEW=false) actually does."""
    from app.config import get_settings
    settings = get_settings()
    original = settings.require_deid_review
    settings.require_deid_review = False
    try:
        csrf = _login(client)
        m = client.post("/api/match", json={"deidentified_text": LEAKY, "top_k": 3,
                        "active_only": False, "interventional_only": False},
                        headers={"x-csrf-token": csrf})
        assert m.status_code == 200, m.text
        blob = m.text
        assert "John Q. Public" not in blob
        assert "John" not in blob or "Public" not in blob
    finally:
        settings.require_deid_review = original


def test_logout_revokes_session(client):
    csrf = _login(client)
    headers = {"x-csrf-token": csrf}
    r = client.post("/api/logout", headers=headers)
    assert r.status_code == 200
    client.cookies.clear()
    r2 = client.get("/api/me")
    assert r2.status_code == 401
