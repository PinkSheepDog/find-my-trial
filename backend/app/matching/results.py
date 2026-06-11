"""Result schema for the ranked trial board — the physician-facing output."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ScoreBreakdown(BaseModel):
    condition: float = 0.0
    biomarker: float = 0.0
    therapy: float = 0.0
    lexical: float = 0.0
    status: float = 0.0
    contraindication_penalty: float = 0.0


class TrialResult(BaseModel):
    rank: int
    nct: str
    title: str
    url: str
    status: str
    phase: str
    study_type: str
    sponsor: str
    brief_summary: str
    conditions: list[str] = Field(default_factory=list)
    interventions: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)

    confidence: float                 # 0-100, calibrated fit (NOT eligibility)
    fit_label: str                    # e.g. "Strong fit", "Promising", "Conditional", "Low fit"
    reasons: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)
    contraindications: list[str] = Field(default_factory=list)

    matched_conditions: list[str] = Field(default_factory=list)
    matched_biomarkers: list[str] = Field(default_factory=list)
    matched_therapies: list[str] = Field(default_factory=list)
    breakdown: ScoreBreakdown = Field(default_factory=ScoreBreakdown)

    eligibility_sex: str = "Not specified"
    eligibility_age: str = "Not specified"

    explained_by: str = "rules"       # "llm" | "rules"


class MatchResponse(BaseModel):
    results: list[TrialResult]
    candidate_count: int
    trial_count: int
    semantic_used: bool
    degraded_mode: bool               # True when no LLM key (rules-only explanations)
    fallback_hint: str | None = None
