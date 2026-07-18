"""The PatientProfile schema — the single structured representation of a patient,
produced by extraction and consumed by retrieval, reranking, and the UI.

Central design decision (the bug fix): a Biomarker is a (name, status) pair, not a
bare string. Status is a closed enum. There is no way to record "BRCA" without
saying whether it is positive, negative, low, or unknown — so the
"negative-read-as-positive" failure mode of the old system cannot occur here.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class BiomarkerStatus(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    LOW = "low"            # e.g. HER2-low (IHC 1+ / 2+ FISH-negative): NOT HER2-positive
    EQUIVOCAL = "equivocal"
    UNKNOWN = "unknown"


class Certainty(str, Enum):
    STATED = "stated"        # explicitly in the chart
    INFERRED = "inferred"    # derived (e.g. TNBC implies ER/PR/HER2 negative)
    UNCERTAIN = "uncertain"  # ambiguous / pending / conflicting in the chart


class BiomarkerTiming(str, Enum):
    """WHEN a biomarker result applies. Current drives matching; historical is retained
    (a prior HER2 result must not silently overwrite the current one), pending is not a
    detected alteration."""
    CURRENT = "current"
    HISTORICAL = "historical"
    PENDING = "pending"
    UNKNOWN = "unknown"


class Evidence(BaseModel):
    """A short verbatim snippet from the (de-identified) chart supporting a field."""
    field: str
    snippet: str


class ReviewState(str, Enum):
    """The reviewable state of a normalized fact (feedback: expose these before matching)."""
    CONFIRMED = "confirmed"      # stated and definitive
    INFERRED = "inferred"        # derived (e.g. TNBC -> ER/PR/HER2 negative)
    CONFLICTING = "conflicting"  # contradictory mentions in the chart
    HISTORICAL = "historical"    # a prior result retained, not current
    NEGATED = "negated"          # explicitly negative / absent
    MISSING = "missing"          # needed for matching but not found / pending / stale


class Fact(BaseModel):
    """A normalized, source-linked patient fact for the reviewable profile. Additive to
    the typed fields the matcher uses — this layer is for human review/correction."""
    fact_type: str                             # e.g. "biomarker.HER2", "stage", "ecog"
    value: str
    review_state: ReviewState
    evidence: str | None = None                # verbatim de-identified snippet
    timing: str | None = None                  # current | historical | pending (where relevant)


class Biomarker(BaseModel):
    name: str                                  # canonical, e.g. "HER2", "BRCA", "PD-L1"
    status: BiomarkerStatus = BiomarkerStatus.UNKNOWN
    detail: str | None = None                  # e.g. "IHC 1+", "FISH not amplified", "15%"
    certainty: Certainty = Certainty.STATED
    timing: BiomarkerTiming = BiomarkerTiming.CURRENT
    specimen: str | None = None                # e.g. "Left lung core biopsy"
    method: str | None = None                  # e.g. "IHC", "ISH", "NGS"
    date: str | None = None                    # effective date if it survived de-id

    @property
    def is_current(self) -> bool:
        return self.timing in {BiomarkerTiming.CURRENT, BiomarkerTiming.UNKNOWN}

    @property
    def is_actionably_positive(self) -> bool:
        """True only when the marker is a CURRENT POSITIVE actionable target. HER2-low,
        negative, historical, and pending are explicitly NOT actionably positive — this
        gates contraindication logic downstream."""
        return self.status == BiomarkerStatus.POSITIVE and self.is_current


class Therapy(BaseModel):
    name: str
    is_current: bool = False
    caused_toxicity: str | None = None         # e.g. "grade 3 immune-mediated hepatitis"


class PatientProfile(BaseModel):
    # Demographics
    age: int | None = None
    sex: str | None = None                     # "Female" | "Male" | None

    # Disease
    diagnosis: str | None = None               # free-text canonical diagnosis line
    cancer_types: list[str] = Field(default_factory=list)  # normalized condition terms
    stage: str | None = None
    is_metastatic: bool = False
    disease_sites: list[str] = Field(default_factory=list)

    # Molecular
    biomarkers: list[Biomarker] = Field(default_factory=list)

    # Treatment
    therapies: list[Therapy] = Field(default_factory=list)

    # Status / constraints
    ecog: int | None = None
    comorbidities: list[str] = Field(default_factory=list)
    organ_function_flags: list[str] = Field(default_factory=list)  # e.g. "CKD II", "mild LFT elevation"

    # Logistics
    location_preferences: list[str] = Field(default_factory=list)

    # Explainability / trust
    evidence: list[Evidence] = Field(default_factory=list)
    missing_or_uncertain: list[str] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)  # reviewable, source-linked fact list

    # Provenance
    extractor: str = "rules"                    # "rules" | "llm"

    # --- convenience accessors used by retrieval/rerank (CURRENT results only) ---
    def positive_biomarkers(self) -> list[Biomarker]:
        return [b for b in self.biomarkers if b.status == BiomarkerStatus.POSITIVE and b.is_current]

    def negative_or_low_biomarkers(self) -> list[Biomarker]:
        return [b for b in self.biomarkers
                if b.status in {BiomarkerStatus.NEGATIVE, BiomarkerStatus.LOW} and b.is_current]

    def biomarker(self, name: str) -> Biomarker | None:
        """The CURRENT reading for a marker if present, else any (historical)."""
        name = name.upper()
        matches = [b for b in self.biomarkers if b.name.upper() == name]
        return next((b for b in matches if b.is_current), matches[0] if matches else None)

    def therapy_names(self) -> list[str]:
        return [t.name for t in self.therapies]

    def summary_line(self) -> str:
        bits: list[str] = []
        demo = " ".join(x for x in [
            f"{self.age}yo" if self.age else None,
            self.sex.lower() if self.sex else None,
        ] if x)
        if demo:
            bits.append(demo)
        if self.stage:
            bits.append(self.stage)
        elif self.is_metastatic:
            bits.append("metastatic")
        if self.diagnosis:
            bits.append(self.diagnosis)
        pos = self.positive_biomarkers()
        if pos:
            bits.append("positive: " + ", ".join(b.name for b in pos))
        neglow = self.negative_or_low_biomarkers()
        if neglow:
            bits.append("neg/low: " + ", ".join(f"{b.name} {b.status.value}" for b in neglow))
        if self.ecog is not None:
            bits.append(f"ECOG {self.ecog}")
        return "; ".join(bits)


def _biomarker_review_state(b: Biomarker) -> ReviewState:
    if b.certainty == Certainty.INFERRED:
        return ReviewState.INFERRED
    if b.certainty == Certainty.UNCERTAIN:
        return ReviewState.CONFLICTING
    if b.timing == BiomarkerTiming.HISTORICAL:
        return ReviewState.HISTORICAL
    if b.timing == BiomarkerTiming.PENDING or b.status == BiomarkerStatus.UNKNOWN:
        return ReviewState.MISSING
    if b.status == BiomarkerStatus.NEGATIVE:
        return ReviewState.NEGATED
    return ReviewState.CONFIRMED


def derive_facts(p: PatientProfile) -> list[Fact]:
    """Build the reviewable, source-linked fact list from a profile's typed fields.
    Works for either extractor and exposes the six review states."""
    ev = {e.field: e.snippet for e in p.evidence}
    facts: list[Fact] = []
    for c in p.cancer_types:
        facts.append(Fact(fact_type="cancer_type", value=c, review_state=ReviewState.CONFIRMED))
    stage_val = p.stage or ("Metastatic" if p.is_metastatic else "not documented")
    facts.append(Fact(fact_type="stage", value=stage_val,
                      review_state=ReviewState.CONFIRMED if (p.stage or p.is_metastatic) else ReviewState.MISSING))
    facts.append(Fact(fact_type="performance_status_ecog",
                      value=str(p.ecog) if p.ecog is not None else "not documented",
                      review_state=ReviewState.CONFIRMED if p.ecog is not None else ReviewState.MISSING))
    for s in p.disease_sites:
        facts.append(Fact(fact_type="metastatic_site", value=s, review_state=ReviewState.CONFIRMED))
    for b in p.biomarkers:
        detail = f" ({b.detail})" if b.detail else ""
        facts.append(Fact(
            fact_type=f"biomarker.{b.name}", value=f"{b.status.value}{detail}",
            review_state=_biomarker_review_state(b),
            evidence=ev.get(f"biomarker:{b.name}"), timing=b.timing.value,
        ))
    for t in p.therapies:
        facts.append(Fact(fact_type="treatment", value=t.name, review_state=ReviewState.CONFIRMED))
    for miss in p.missing_or_uncertain:
        facts.append(Fact(fact_type="review_item", value=miss, review_state=ReviewState.MISSING))
    return facts
