"""Tests for the server-side de-identification egress gate.

The audit these fix found that FMT_REQUIRE_DEID_REVIEW appeared exactly once in
the codebase — its own declaration. Nothing read it. The review gate lived only
in React state, so any authenticated caller could POST raw chart text straight to
/api/match. The project's own test suite did exactly that.

These tests pin the gate shut.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.intake.deident import deidentify
from app.security.deid_gate import ApprovalError, issue_approval, verify_approval

CLEAN = "Patient: [NAME], [AGE] F, metastatic breast cancer, HER2 IHC 1+, ECOG 1."
OTHER = "Patient: [NAME], [AGE] M, metastatic pancreatic cancer, ECOG 2."


def _settings(**kw) -> Settings:
    base = dict(secret_key="test-secret-key-long-enough", deid_approval_ttl_minutes=30)
    base.update(kw)
    return Settings(**base)


def test_valid_approval_passes():
    s = _settings()
    verify_approval(s, issue_approval(s, CLEAN), CLEAN)  # must not raise


def test_missing_token_is_refused():
    s = _settings()
    with pytest.raises(ApprovalError, match="not been approved"):
        verify_approval(s, None, CLEAN)
    with pytest.raises(ApprovalError):
        verify_approval(s, "", CLEAN)


def test_forged_token_is_refused():
    s = _settings()
    with pytest.raises(ApprovalError, match="not valid"):
        verify_approval(s, "not-a-real-token", CLEAN)


def test_token_from_a_different_secret_is_refused():
    issued = issue_approval(_settings(secret_key="secret-number-one-long"), CLEAN)
    with pytest.raises(ApprovalError, match="not valid"):
        verify_approval(_settings(secret_key="secret-number-two-long"), issued, CLEAN)


def test_token_bound_to_other_text_is_refused():
    """The core anti-bypass property: approving innocuous text must not license
    matching a different chart under the same token."""
    s = _settings()
    token = issue_approval(s, CLEAN)
    with pytest.raises(ApprovalError, match="does not match"):
        verify_approval(s, token, OTHER)


def test_expired_token_is_refused(monkeypatch):
    """Approval must go stale, so a token cannot license egress indefinitely.
    Time is advanced rather than using a zero TTL, because a token signed in the
    same instant has age 0 and `0 > 0` is false — a zero TTL would not expire it."""
    import itsdangerous.timed as timed

    s = _settings(deid_approval_ttl_minutes=30)
    token = issue_approval(s, CLEAN)
    verify_approval(s, token, CLEAN)  # valid right now

    real_time = timed.time.time
    monkeypatch.setattr(timed.time, "time", lambda: real_time() + 31 * 60)
    with pytest.raises(ApprovalError, match="expired"):
        verify_approval(s, token, CLEAN)


def test_whitespace_only_edits_do_not_invalidate_approval():
    """A trailing newline from a textarea must not force a re-review, but any
    substantive edit must."""
    s = _settings()
    token = issue_approval(s, CLEAN)
    verify_approval(s, token, CLEAN + "\n")
    verify_approval(s, token, "  " + CLEAN + "  ")
    with pytest.raises(ApprovalError):
        verify_approval(s, token, CLEAN + " Also ECOG 3.")


def test_digest_carries_no_patient_text():
    """The token is handed to the client, so it must not embed chart content."""
    token = issue_approval(_settings(), CLEAN)
    for fragment in ("breast", "HER2", "ECOG", "Patient", "metastatic"):
        assert fragment not in token


def test_deidentifier_is_idempotent():
    """The approval endpoint refuses text with residual identifiers, which only
    works if a second pass over scrubbed text finds nothing. If de-identification
    ever stops being idempotent, approval would reject its own output and the
    whole flow would deadlock — so this is a load-bearing property, not a nicety."""
    raw = ("Patient: Maria Gonzalez, DOB 1958-09-02, MRN 883-22-9910. "
           "57yo F with metastatic breast cancer, HER2 IHC 1+, ECOG 1.")
    first = deidentify(raw)
    second = deidentify(first.text)
    assert first.total_redactions > 0, "fixture should contain identifiers"
    assert second.total_redactions == 0, "second pass must find nothing to redact"
    assert first.text == second.text
