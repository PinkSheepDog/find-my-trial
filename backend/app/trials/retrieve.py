"""Candidate retrieval: PatientProfile -> ranked shortlist of TrialRecords.

Two stages, both LLM-free and cheap:
  1. HARD FILTERS (structured eligibility): sex, age bucket, status, study type.
     These are facts, not guesses — wrong-sex or wrong-age trials are removed.
  2. BM25 relevance over the survivors, using a query built from the patient's
     POSITIVE signals only (diagnosis, sites, positive biomarkers, therapy class).
     Negative/low biomarkers are deliberately NOT used to boost matches.

The output is a candidate list (default top ~40) handed to the reranker. Each
candidate carries the signals the reranker needs to both SCORE and EXPLAIN the
decision, with verbatim evidence:

  * `contraindications` — trial requires a biomarker direction the patient lacks.
  * `eligibility_conflicts` — the patient has a condition/performance status the
    trial's own exclusion language rules out.
  * `disease_unclassified` — the trial names no recognized cancer family, so it never
    actually faced the disease gate; it is FLAGGED and demoted, not exempted.
  * `location_match` — whether any trial site matches the requested geography.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.extraction.schema import BiomarkerStatus, PatientProfile
from app.matching.clinical import (
    NON_TREATMENT_PURPOSES,
    TREATMENT_PURPOSES,
    EligibilityConflict,
    basket_evidence,
    disease_families_of,
    eligibility_conflicts,
    is_oncology_text,
    refine_purpose,
    term_in_text,
)
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
    disease_family: str = ""      # patient∩trial family that let it through the gate
    study_purpose: str = "unknown"
    # --- gate provenance ---
    disease_unclassified: bool = False   # trial names no recognized cancer family
    basket_evidence: str = ""            # verbatim basket phrase, when admitted as a basket
    purpose_evidence: str = ""           # keyword that drove an inferred purpose
    purpose_unverified: bool = False     # no purpose stated by the registry and none inferred
    # --- geography ---
    location_query: str = ""
    matched_locations: list[str] = field(default_factory=list)
    # --- eligibility ---
    eligibility_conflicts: list[EligibilityConflict] = field(default_factory=list)

    @property
    def location_match(self) -> bool:
        return bool(self.matched_locations)


@dataclass
class RetrievalFilters:
    active_only: bool = True
    # "Active" includes ACTIVE_NOT_RECRUITING (a study still running but CLOSED to new
    # patients). `recruiting_only` is the stricter, honestly-named filter: studies a
    # patient could actually enrol in today.
    recruiting_only: bool = False
    interventional_only: bool = True
    treatment_only: bool = True   # drop diagnostic/screening/registry/observational studies
    location: str = ""
    # Geography is a strong RANKING signal by default, not a hard filter: a hard filter
    # on a corpus with sparse site data empties the board silently. Set
    # `location_required` to make it a hard filter when the clinician means it.
    location_required: bool = False


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

# Demotion applied to a trial that never faced the disease gate (no recognized family).
_UNCLASSIFIED_DISEASE_FACTOR = 0.45
# Demotion applied when the registry states no primary purpose and none can be inferred.
_UNVERIFIED_PURPOSE_FACTOR = 0.75
# Geography: a match is a strong boost, a miss when geography was requested is a demotion.
_LOCATION_BOOST = 0.18
_LOCATION_MISS_FACTOR = 0.72

_US_STATE_ABBREV = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire",
    "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina",
    "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee",
    "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
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

    pos_biomarkers = {b.name.upper() for b in profile.positive_biomarkers()}
    neg_low = {b.name.upper(): b.status for b in profile.negative_or_low_biomarkers()}
    patient_families = patient_disease_families(profile)
    # Geography: the explicit filter wins; otherwise fall back to what the chart said.
    location_query = (filters.location or "").strip() or ", ".join(profile.location_preferences).strip()
    location_terms = _location_terms(location_query)
    # Conditions the trial might exclude: comorbidities + organ-function flags.
    patient_conditions = list(profile.comorbidities) + list(profile.organ_function_flags)

    candidates: list[Candidate] = []
    for i, rec in enumerate(index.records):
        if not _passes_hard_filters(rec, profile, filters):
            continue

        # --- Clinical GATES (before any biomarker/lexical overlap is rewarded) ---
        # Purpose gate: when treatment studies are requested, drop imaging / screening /
        # prevention / registry / observational studies outright. An INTERVENTIONAL study
        # with no stated purpose is "unknown" — kept, but flagged and demoted, never
        # silently treated as a treatment study.
        purpose, purpose_kw = refine_purpose(rec.study_purpose, rec.title + " " + rec.brief_summary)
        if filters.treatment_only and purpose in NON_TREATMENT_PURPOSES:
            continue
        purpose_unverified = purpose not in TREATMENT_PURPOSES and purpose not in NON_TREATMENT_PURPOSES

        # Disease-family gate: drop wrong-primary-cancer studies. Exception: a
        # tumour-agnostic basket, or a trial naming a biomarker the patient is positive
        # for (a biomarker-defined basket), may still apply across tumour types.
        rec_low = (rec.title + " " + rec.condition_text).lower()
        gate_family = ""
        disease_unclassified = False
        basket_ev = ""
        if patient_families:
            shared = patient_families & rec.disease_families
            if shared:
                gate_family = ", ".join(sorted(shared))
            else:
                is_biomarker_basket = any(m.lower() in rec_low for m in pos_biomarkers)
                if rec.is_basket or is_biomarker_basket:
                    basket_ev = basket_evidence(rec.title + " " + rec.condition_text)
                elif rec.disease_families:
                    continue  # wrong primary cancer, not a basket -> reject
                elif not is_oncology_text(rec_low + " " + rec.brief_summary):
                    # No recognized family AND no cancer language at all: not an oncology
                    # study. Previously these skipped the gate entirely and competed on
                    # BM25 alone — the gate's largest silent escape hatch.
                    continue
                else:
                    # Oncology-ish but unclassifiable: keep, FLAG and demote. It must not
                    # rank as though it had passed a gate it never faced.
                    disease_unclassified = True

        rec_blob = (rec.title + " " + rec.condition_text + " " + rec.brief_summary).lower()
        matched_conditions = [c for c in profile.cancer_types if c.lower() in rec_blob]
        norm_bm25 = bm25[i] / bm25_max
        if norm_bm25 <= 0 and not matched_conditions:
            continue  # no lexical or condition signal at all -> not a candidate

        matched_biomarkers = [m for m in pos_biomarkers if m.lower() in rec_blob]
        matched_therapies = [t for t in profile.therapy_names() if t.lower() in rec_blob]
        matched_locations = _matching_sites(rec, location_terms)
        if filters.location_required and location_terms and not matched_locations:
            continue

        structured = _structured_overlap(
            matched_conditions, matched_biomarkers, matched_therapies, profile
        )
        contraindications = _contraindications(rec_blob, pos_biomarkers, neg_low)
        conflicts = eligibility_conflicts(
            rec.brief_summary, source_field="brief_summary",
            patient_conditions=patient_conditions, ecog=profile.ecog,
        )

        score = 0.55 * norm_bm25 + 0.45 * structured
        # A trial that requires a marker the patient is negative/low for is demoted,
        # but still surfaced (decision support shows near-misses with a warning).
        if contraindications:
            score *= 0.4
        if disease_unclassified:
            score *= _UNCLASSIFIED_DISEASE_FACTOR
        if purpose_unverified:
            score *= _UNVERIFIED_PURPOSE_FACTOR
        if location_terms:
            if matched_locations:
                score += _LOCATION_BOOST
            else:
                score *= _LOCATION_MISS_FACTOR

        candidates.append(Candidate(
            record=rec, bm25=norm_bm25, structured_overlap=structured, score=score,
            matched_conditions=matched_conditions, matched_biomarkers=matched_biomarkers,
            matched_therapies=matched_therapies, contraindications=contraindications,
            disease_family=gate_family, study_purpose=purpose,
            disease_unclassified=disease_unclassified, basket_evidence=basket_ev,
            purpose_evidence=purpose_kw, purpose_unverified=purpose_unverified,
            location_query=location_query, matched_locations=matched_locations,
            eligibility_conflicts=conflicts,
        ))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:top_k]


def patient_disease_families(profile: PatientProfile) -> frozenset[str]:
    """The patient's primary cancer family/families, from diagnosis + cancer types only
    (NOT metastatic sites, so a liver metastasis is never read as liver cancer)."""
    parts = list(profile.cancer_types)
    if profile.diagnosis:
        parts.append(profile.diagnosis)
    return disease_families_of(" ".join(parts))


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


def _location_terms(location: str) -> list[str]:
    """Split a free-text geography ("Detroit, Michigan", "MI") into match terms.

    A two-letter US state abbreviation is expanded to the full state name, because trial
    site strings spell states out ("..., Detroit, Michigan, 48201, United States")."""
    terms: list[str] = []
    for raw in re.split(r"[,/;|]| and ", location or ""):
        t = raw.strip().lower()
        if len(t) < 2:
            continue
        # A 2-letter US state code is replaced by the spelled-out state, because trial
        # sites spell states out ("..., Detroit, Michigan, 48201, United States") and a
        # bare 2-letter substring match ("MI") hits Miami, Memorial, Mid-Florida...
        terms.append(_US_STATE_ABBREV.get(t, t) if len(t) == 2 else t)
    return list(dict.fromkeys(terms))


def _matching_sites(rec: TrialRecord, terms: list[str]) -> list[str]:
    """Trial sites (verbatim) that match any requested geography term."""
    if not terms:
        return []
    hits: list[str] = []
    for site in rec.locations:
        if any(term_in_text(t, site) for t in terms):
            hits.append(site)
        if len(hits) >= 3:
            break
    return hits


def _passes_hard_filters(rec: TrialRecord, profile: PatientProfile, filters: RetrievalFilters) -> bool:
    # `recruiting_only` is strictly stronger than `active_only`: it excludes
    # ACTIVE_NOT_RECRUITING, which is running but closed to new participants.
    if filters.recruiting_only and not rec.is_recruiting:
        return False
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
