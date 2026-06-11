"""Candidate retrieval: PatientProfile -> ranked shortlist of TrialRecords.

Two stages, both LLM-free and cheap:
  1. HARD FILTERS (structured eligibility): sex, age bucket, status, study type.
     These are facts, not guesses — wrong-sex or wrong-age trials are removed.
  2. BM25 relevance over the survivors, using a query built from the patient's
     POSITIVE signals only (diagnosis, sites, positive biomarkers, therapy class).
     Negative/low biomarkers are deliberately NOT used to boost matches.

The output is a candidate list (default top ~40) handed to the reranker. A
`contraindication` flag is attached per candidate so the reranker and UI can warn
when a trial's title/conditions require a biomarker the patient is negative/low for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.extraction.schema import BiomarkerStatus, PatientProfile
from app.trials.index import TrialIndex, TrialRecord, _tokenize


@dataclass
class Candidate:
    record: TrialRecord
    bm25: float
    structured_overlap: float
    score: float
    matched_conditions: list[str]
    matched_biomarkers: list[str]
    matched_therapies: list[str]
    contraindications: list[str]  # e.g. "Trial appears to require HER2-positive; patient is HER2-low"


@dataclass
class RetrievalFilters:
    active_only: bool = True
    interventional_only: bool = True
    location: str = ""


# Map a biomarker to the phrases that, in a trial title/conditions, imply the trial
# REQUIRES that marker positive. Used purely to flag contraindications, never to hide.
_REQUIRES_POSITIVE = {
    "HER2": [r"her2[\s\-]?positive", r"her2\+", r"her2 amplified"],
    "EGFR": [r"egfr[\s\-]?mutant", r"egfr[\s\-]?positive", r"egfr mutation"],
    "ER": [r"\ber[\s\-]?positive", r"hormone receptor positive", r"hr[\s\-]?positive"],
    "BRCA": [r"brca[\s\-]?mutant", r"brca[\s\-]?positive", r"germline brca"],
    "ALK": [r"alk[\s\-]?positive", r"alk[\s\-]?rearrang"],
}
_REQUIRES_NEGATIVE = {
    "HER2": [r"her2[\s\-]?negative"],
    "ER": [r"\ber[\s\-]?negative", r"hormone receptor negative", r"triple[\s\-]?negative"],
}


def retrieve(
    profile: PatientProfile,
    index: TrialIndex,
    *,
    filters: RetrievalFilters | None = None,
    top_k: int = 40,
) -> list[Candidate]:
    filters = filters or RetrievalFilters()
    query_tokens = _build_query_tokens(profile)
    bm25 = index.bm25_scores(query_tokens) if query_tokens else [0.0] * len(index.records)
    bm25_max = max(bm25) if len(bm25) and max(bm25) > 0 else 1.0

    pos_conditions = {c.lower() for c in profile.cancer_types}
    pos_biomarkers = {b.name.upper() for b in profile.positive_biomarkers()}
    neg_low = {b.name.upper(): b.status for b in profile.negative_or_low_biomarkers()}
    therapies = {t.lower() for t in profile.therapy_names()}

    candidates: list[Candidate] = []
    for i, rec in enumerate(index.records):
        if not _passes_hard_filters(rec, profile, filters):
            continue

        rec_blob = (rec.title + " " + rec.condition_text + " " + rec.brief_summary).lower()
        matched_conditions = [c for c in profile.cancer_types if c.lower() in rec_blob]
        matched_biomarkers = [m for m in pos_biomarkers if m.lower() in rec_blob]
        matched_therapies = [t for t in profile.therapy_names() if t.lower() in rec_blob]

        structured = _structured_overlap(
            matched_conditions, matched_biomarkers, matched_therapies, profile
        )
        contraindications = _contraindications(rec_blob, pos_biomarkers, neg_low)

        norm_bm25 = bm25[i] / bm25_max
        score = 0.55 * norm_bm25 + 0.45 * structured
        # A trial that requires a marker the patient is negative/low for is demoted,
        # but still surfaced (decision support shows near-misses with a warning).
        if contraindications:
            score *= 0.4

        if norm_bm25 <= 0 and not matched_conditions:
            continue  # no lexical or condition signal at all -> not a candidate

        candidates.append(Candidate(
            record=rec, bm25=norm_bm25, structured_overlap=structured, score=score,
            matched_conditions=matched_conditions, matched_biomarkers=matched_biomarkers,
            matched_therapies=matched_therapies, contraindications=contraindications,
        ))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:top_k]


def _build_query_tokens(profile: PatientProfile) -> list[str]:
    parts: list[str] = []
    parts += profile.cancer_types
    parts += profile.disease_sites
    parts += [b.name for b in profile.positive_biomarkers()]  # positive markers only
    parts += profile.therapy_names()
    if profile.is_metastatic:
        parts.append("metastatic advanced")
    if profile.diagnosis:
        parts.append(profile.diagnosis)
    return _tokenize(" ".join(parts))


def _passes_hard_filters(rec: TrialRecord, profile: PatientProfile, filters: RetrievalFilters) -> bool:
    if filters.active_only and not rec.is_active:
        return False
    if filters.interventional_only and not rec.is_interventional:
        return False
    # Sex eligibility
    if profile.sex and rec.sex not in {"ALL", "NA", ""}:
        if not rec.sex.startswith(profile.sex.upper()[0]):
            return False
    # Age bucket eligibility
    if profile.age is not None and rec.age_buckets:
        bucket = _age_bucket(profile.age)
        if bucket and "NA" not in rec.age_buckets and bucket not in rec.age_buckets:
            return False
    return True


def _age_bucket(age: int) -> str | None:
    if age < 18:
        return "CHILD"
    if age < 65:
        return "ADULT"
    return "OLDER_ADULT"


def _structured_overlap(conditions, biomarkers, therapies, profile: PatientProfile) -> float:
    score = 0.0
    if profile.cancer_types:
        score += 0.6 * (len(conditions) / max(len(profile.cancer_types), 1))
    if profile.positive_biomarkers():
        score += 0.25 * (len(biomarkers) / max(len(profile.positive_biomarkers()), 1))
    if profile.therapies:
        score += 0.15 * min(len(therapies) / max(len(profile.therapies), 1), 1.0)
    return min(score, 1.0)


def _contraindications(rec_blob: str, pos_biomarkers: set[str], neg_low: dict[str, BiomarkerStatus]) -> list[str]:
    out: list[str] = []
    # Trial requires a marker positive, but patient is negative/low for it.
    for marker, status in neg_low.items():
        for pat in _REQUIRES_POSITIVE.get(marker, []):
            if re.search(pat, rec_blob):
                out.append(
                    f"Trial appears to require {marker}-positive; patient is {marker} {status.value}."
                )
                break
    # Trial requires a marker negative, but patient is positive for it.
    for marker in pos_biomarkers:
        for pat in _REQUIRES_NEGATIVE.get(marker, []):
            if re.search(pat, rec_blob):
                out.append(
                    f"Trial appears to require {marker}-negative; patient is {marker}-positive."
                )
                break
    return out
