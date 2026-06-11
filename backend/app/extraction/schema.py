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


class Evidence(BaseModel):
    """A short verbatim snippet from the (de-identified) chart supporting a field."""
    field: str
    snippet: str


class Biomarker(BaseModel):
    name: str                                  # canonical, e.g. "HER2", "BRCA", "PD-L1"
    status: BiomarkerStatus = BiomarkerStatus.UNKNOWN
    detail: str | None = None                  # e.g. "IHC 1+", "FISH not amplified", "15%"
    certainty: Certainty = Certainty.STATED

    @property
    def is_actionably_positive(self) -> bool:
        """True only when the marker is a POSITIVE actionable target. HER2-low and
        negative are explicitly NOT actionably positive — this gates contraindication
        logic downstream."""
        return self.status == BiomarkerStatus.POSITIVE


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

    # Provenance
    extractor: str = "rules"                    # "rules" | "llm"

    # --- convenience accessors used by retrieval/rerank ---
    def positive_biomarkers(self) -> list[Biomarker]:
        return [b for b in self.biomarkers if b.status == BiomarkerStatus.POSITIVE]

    def negative_or_low_biomarkers(self) -> list[Biomarker]:
        return [b for b in self.biomarkers
                if b.status in {BiomarkerStatus.NEGATIVE, BiomarkerStatus.LOW}]

    def biomarker(self, name: str) -> Biomarker | None:
        name = name.upper()
        for b in self.biomarkers:
            if b.name.upper() == name:
                return b
        return None

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
