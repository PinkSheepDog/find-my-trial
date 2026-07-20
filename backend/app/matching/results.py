"""Result schema for the ranked trial board — the physician-facing output.

BREAKING SHAPE CHANGE (explainability): `reasons` and `cautions` are no longer
`list[str]`. They are `list[Explanation]`, where every entry carries the VERBATIM
snippet from the trial record that supports it and the field that snippet came from.
A synthesized label with no quote behind it ("Primary disease match: breast cancer")
is not evidence; a clinician cannot check it without leaving the app. Explanations
whose evidence cannot be found literally in the trial record are marked
`grounded=False` (deterministic path) or dropped outright (LLM path).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Explanation(BaseModel):
    """One reason or caution, with the trial text that backs it.

    `evidence_snippet` is copied VERBATIM out of `source_field` — never paraphrased,
    never synthesized. An empty snippet means the claim is derived from the patient
    profile or from structured registry metadata rather than quotable trial prose;
    `source_field` then says which (e.g. "patient_profile", "status")."""
    text: str
    evidence_snippet: str = ""
    source_field: str = ""     # title | conditions | brief_summary | status | locations |
                               # study_design | interventions | patient_profile
    grounded: bool = True      # False = evidence could NOT be verified in the trial record


class ScoreBreakdown(BaseModel):
    condition: float = 0.0
    biomarker: float = 0.0
    therapy: float = 0.0
    lexical: float = 0.0
    status: float = 0.0
    disease: float = 0.0               # disease-family gate signal (P0)
    purpose: float = 0.0               # study-purpose gate signal (P0)
    location: float = 0.0              # geography boost / miss penalty (signed)
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

    match_score: float                # 0-100 FIT / review priority — NOT an eligibility probability
    fit_label: str                    # e.g. "Strong fit", "Promising", "Conditional", "Low fit"
    reasons: list[Explanation] = Field(default_factory=list)
    cautions: list[Explanation] = Field(default_factory=list)
    contraindications: list[str] = Field(default_factory=list)
    # Flattened, de-duplicated view of every grounded quote backing this card — for
    # export/handoff, where the clinician wants the sources in one place.
    evidence: list[Explanation] = Field(default_factory=list)
    # LLM explanations discarded because their claimed evidence was not in the record.
    ungrounded_dropped: int = 0

    matched_conditions: list[str] = Field(default_factory=list)
    matched_biomarkers: list[str] = Field(default_factory=list)
    matched_therapies: list[str] = Field(default_factory=list)
    breakdown: ScoreBreakdown = Field(default_factory=ScoreBreakdown)

    eligibility_sex: str = "Not specified"
    eligibility_age: str = "Not specified"

    # Clinical gate evidence (why this trial cleared disease + purpose gating).
    disease_family: str = ""          # patient∩trial cancer family
    study_purpose: str = "unknown"    # treatment | diagnostic | screening | observational | ...
    disease_unclassified: bool = False  # trial named no recognized cancer family (flagged, demoted)
    purpose_unverified: bool = False    # registry stated no primary purpose and none inferred

    # Geography
    location_match: bool = False               # a listed site matched the requested location
    matched_locations: list[str] = Field(default_factory=list)
    is_recruiting: bool = False                # status is genuinely open to new patients

    explained_by: str = "rules"       # "llm" | "rules"


class MatchResponse(BaseModel):
    results: list[TrialResult]
    candidate_count: int
    trial_count: int
    semantic_used: bool
    degraded_mode: bool               # True when no LLM key (rules-only explanations)
    fallback_hint: str | None = None
    # Geography feedback: the location that was applied (from the filter or the chart)
    # and how many results have a site there. `location_notice` is the user-visible
    # "no sites near X" caution — the filter is a strong boost, not a hard filter, so
    # the board must say when nothing nearby was found instead of implying it did.
    location_query: str = ""
    location_match_count: int = 0
    location_notice: str | None = None
    # Abstention: when core facts are missing the system returns NEEDS-REVIEW rather
    # than a confident ranked list (decision support must not over-claim on thin input).
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
