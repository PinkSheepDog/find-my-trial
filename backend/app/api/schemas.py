"""Request/response models for the API layer."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.extraction.schema import PatientProfile
from app.matching.results import MatchResponse


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
    active_only: bool = True
    interventional_only: bool = True
    treatment_only: bool = True   # gate out diagnostic/screening/registry/observational studies
    location: str = ""


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
