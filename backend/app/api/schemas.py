"""Request/response models for the API layer."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.extraction.schema import PatientProfile
from app.matching.results import MatchResponse
from app.trials.retrieve import RetrievalFilters


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    username: str
    csrf_token: str


class DeidRequest(BaseModel):
    """Step 1: client sends raw chart text; server returns DE-IDENTIFIED text plus
    a redaction summary for the human review-before-send gate. The server does NOT
    store the raw text and does NOT call the LLM here."""
    text: str = Field(min_length=1)


class DeidResponse(BaseModel):
    deidentified_text: str
    redaction_summary: str
    redaction_counts: dict[str, int]
    total_redactions: int


class MatchRequest(BaseModel):
    """Step 2: client sends the APPROVED de-identified text (the only text allowed
    to reach the LLM). Raw chart text is never accepted by this endpoint."""
    deidentified_text: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1, le=30)
    # "Active" includes ACTIVE_NOT_RECRUITING — a study still running but CLOSED to new
    # participants. It is NOT the same as "open to enrolment".
    active_only: bool = True
    # Genuinely open to new patients (RECRUITING / NOT_YET_RECRUITING /
    # ENROLLING_BY_INVITATION / AVAILABLE). Strictly stronger than `active_only`.
    recruiting_only: bool = False
    interventional_only: bool = True
    treatment_only: bool = True   # gate out diagnostic/screening/registry/observational studies
    location: str = ""            # free text, e.g. "Detroit, Michigan" or "MI"
    # When false (default) location is a strong RANKING boost and a "no sites near X"
    # caution; when true it becomes a hard filter and can legitimately empty the board.
    location_required: bool = False

    def to_retrieval_filters(self) -> RetrievalFilters:
        """The single place request filters become retrieval filters — so a new filter
        cannot be added to the API and silently never reach retrieval (which is exactly
        how `location` stayed dead)."""
        return RetrievalFilters(
            active_only=self.active_only,
            recruiting_only=self.recruiting_only,
            interventional_only=self.interventional_only,
            treatment_only=self.treatment_only,
            location=self.location,
            location_required=self.location_required,
        )


class MatchResult(BaseModel):
    profile: PatientProfile
    match: MatchResponse


class HealthResponse(BaseModel):
    ok: bool
    trial_count: int
    llm_enabled: bool
    degraded_mode: bool
    data_current_through: str = ""     # latest trial "Last Update Posted" in the corpus
    normalization_version: str = ""    # index/normalization revision
    # --- build / provenance diagnostics ---
    app_version: str = ""
    corpus_content_hash: str | None = None   # which corpus produced this board
    index_built_at: str | None = None
    # False means the corpus was accepted without a digest check — surfaced so the
    # UI can say "unverified" rather than implying provenance was confirmed.
    corpus_integrity_verified: bool = False
    # Whether the server enforces the de-identification approval gate.
    deid_review_enforced: bool = True
