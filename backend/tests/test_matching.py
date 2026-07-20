"""Unit tests for the clinical gates, scoring and EVIDENCE layer.

These run on synthetic TrialRecords, so they are fast and do not need the 33MB
corpus. Each test names the defect it locks down:

  * disease gate no longer exempts trials with no recognized family (item 2)
  * purpose defaults conservative + is inferred from text (item 3)
  * basket detection is tumour-agnostic language only (item 4)
  * disease family and study purpose are WEIGHTED components of match_score (item 6)
  * every reason/caution carries a verbatim quote; ungrounded LLM claims are dropped (item 7)
  * exclusion criteria are checked against comorbidities / organ flags / ECOG (item 8)
"""

from __future__ import annotations

import pytest

from app.extraction.schema import (
    Biomarker,
    BiomarkerStatus,
    PatientProfile,
    Therapy,
)
from app.matching.clinical import (
    NON_TREATMENT_PURPOSES,
    TREATMENT_PURPOSES,
    disease_families_of,
    ecog_ceiling,
    eligibility_conflicts,
    grounded_source,
    infer_purpose_from_text,
    is_basket_text,
    is_oncology_text,
    primary_purpose,
    refine_purpose,
    snippet_for,
    term_in_text,
)
from app.matching.rerank import DeterministicReranker, LLMReranker, _ground_llm_items
from app.matching.results import Explanation
from app.trials.index import TrialRecord
from app.trials.retrieve import Candidate


# --------------------------------------------------------------------------- helpers

def make_record(**kw) -> TrialRecord:
    conditions = kw.pop("conditions", ["Breast Cancer"])
    title = kw.pop("title", "A Study of Drug X in HER2-positive Breast Cancer")
    summary = kw.pop("summary", "This interventional study evaluates Drug X.")
    condition_text = " ".join(conditions)
    rec = TrialRecord(
        nct=kw.pop("nct", "NCT00000001"), title=title, url="https://example/1",
        status=kw.pop("status", "RECRUITING"), phase=kw.pop("phase", "PHASE2"),
        study_type=kw.pop("study_type", "INTERVENTIONAL"), sponsor="Sponsor",
        brief_summary=summary, conditions=conditions,
        interventions=kw.pop("interventions", ["Drug X"]),
        locations=kw.pop("locations", ["Karmanos Cancer Institute, Detroit, Michigan, 48201, United States"]),
        sex=kw.pop("sex", "ALL"), age_buckets=kw.pop("age_buckets", {"ADULT", "OLDER_ADULT"}),
        condition_text=condition_text, intervention_text="Drug X",
        is_interventional=True,
        search_text=" ".join([title, summary, condition_text]),
        tokens=[],
        disease_families=kw.pop("disease_families", frozenset({"breast cancer"})),
        study_purpose=kw.pop("study_purpose", "treatment"),
        is_basket=kw.pop("is_basket", False),
    )
    assert not kw, f"unused kwargs {kw}"
    return rec


def make_candidate(rec: TrialRecord, **kw) -> Candidate:
    return Candidate(
        record=rec, bm25=kw.pop("bm25", 0.5), structured_overlap=kw.pop("structured", 0.6),
        score=kw.pop("score", 0.5),
        matched_conditions=kw.pop("matched_conditions", ["breast cancer"]),
        matched_biomarkers=kw.pop("matched_biomarkers", ["HER2"]),
        matched_therapies=kw.pop("matched_therapies", []),
        contraindications=kw.pop("contraindications", []),
        disease_family=kw.pop("disease_family", "breast cancer"),
        study_purpose=kw.pop("study_purpose", "treatment"),
        disease_unclassified=kw.pop("disease_unclassified", False),
        basket_evidence=kw.pop("basket_evidence", ""),
        purpose_evidence=kw.pop("purpose_evidence", ""),
        purpose_unverified=kw.pop("purpose_unverified", False),
        location_query=kw.pop("location_query", ""),
        matched_locations=kw.pop("matched_locations", []),
        eligibility_conflicts=kw.pop("eligibility_conflicts", []),
    )


@pytest.fixture
def profile() -> PatientProfile:
    return PatientProfile(
        age=62, sex="Female", diagnosis="Metastatic HER2-positive breast cancer",
        cancer_types=["breast cancer"], is_metastatic=True,
        biomarkers=[Biomarker(name="HER2", status=BiomarkerStatus.POSITIVE)],
        therapies=[Therapy(name="Trastuzumab")], ecog=1,
    )


# --------------------------------------------------------------- item 4: basket scope

def test_advanced_cancer_is_not_a_basket_study():
    """Regression: 'advanced cancer' / 'advanced malignancies' appear in ordinary
    SINGLE-tumour titles. Treating them as baskets made such trials cross-tumour
    eligible for every patient, defeating the disease gate."""
    assert not is_basket_text("Drug X in Patients With Advanced Cancer of the Breast")
    assert not is_basket_text("A Phase 1 Study in Advanced Malignancies of the Pancreas")
    assert not is_basket_text("Advanced malignancy of the prostate")


def test_genuine_basket_language_still_detected():
    for text in [
        "Drug X in Patients With Advanced Solid Tumors",
        "A Basket Study of Drug Y",
        "Tumor-agnostic therapy for NTRK fusion cancers",
        "Treatment regardless of tumor type",
        "Histology-independent evaluation of Drug Z",
        "Pan-tumor evaluation of Drug Q",
    ]:
        assert is_basket_text(text), text


# ------------------------------------------------------- item 2: disease gate coverage

def test_disease_family_coverage_expanded():
    """Families the original 23-entry table missed entirely — each miss was a trial
    that skipped the disease gate."""
    assert "mesothelioma" in disease_families_of("Malignant Pleural Mesothelioma")
    assert "neuroendocrine tumor" in disease_families_of("Metastatic Carcinoid Tumor")
    assert "non-melanoma skin cancer" in disease_families_of("Merkel Cell Carcinoma")
    assert "germ cell tumor" in disease_families_of("Testicular Cancer, Seminoma")
    assert "cns tumor" in disease_families_of("Recurrent Medulloblastoma")
    assert "myeloproliferative neoplasm" in disease_families_of("Primary Myelofibrosis")


def test_oncology_language_detection_splits_the_unclassified_bucket():
    assert is_oncology_text("Advanced Neoplasms of Unspecified Site")
    assert is_oncology_text("Solid malignancies")
    assert not is_oncology_text("Schizophrenia and NMDA-enhancing agents")
    assert not is_oncology_text("Bioequivalence of Losartan Potassium in Healthy Volunteers")


# ------------------------------------------------------------- item 3: purpose default

def test_interventional_without_stated_purpose_is_unknown_not_treatment():
    """Was: any INTERVENTIONAL study with no Primary Purpose field defaulted to
    'treatment' and cleared the treatment gate."""
    assert primary_purpose("INTERVENTIONAL", "Allocation: RANDOMIZED|Masking: NONE") == "unknown"
    assert primary_purpose("INTERVENTIONAL", "Primary Purpose: TREATMENT") == "treatment"
    assert primary_purpose("OBSERVATIONAL", "") == "observational"


def test_unknown_purpose_is_not_whitelisted_as_treatment():
    assert "unknown" not in TREATMENT_PURPOSES
    assert TREATMENT_PURPOSES == {"treatment", "expanded_access"}


def test_purpose_inferred_from_text_when_registry_states_none():
    assert refine_purpose("unknown", "A PET imaging study of tracer uptake")[0] == "diagnostic"
    assert refine_purpose("unknown", "A prospective registry of treated patients")[0] == "observational"
    assert refine_purpose("unknown", "Natural history of the disease")[0] == "observational"
    assert refine_purpose("unknown", "Cancer screening program in rural clinics")[0] == "screening"
    for purpose in ("diagnostic", "observational", "screening"):
        assert purpose in NON_TREATMENT_PURPOSES


def test_stated_purpose_always_wins_over_text_inference():
    """A genuine treatment trial that merely mentions imaging must not be reclassified."""
    assert refine_purpose("treatment", "Response assessed by PET imaging study")[0] == "treatment"


def test_infer_purpose_returns_the_matched_keyword_as_evidence():
    purpose, kw = infer_purpose_from_text("This is an observational cohort of survivors")
    assert purpose == "observational"
    assert kw and kw in "this is an observational cohort of survivors"


# ------------------------------------------------------------- item 7: evidence layer

def test_snippet_for_returns_verbatim_text_or_nothing():
    text = "A Study of Drug X in HER2-positive Breast Cancer"
    snippet = snippet_for("HER2", text)
    assert "HER2-positive" in snippet
    assert snippet_for("EGFR", text) == ""   # never invents evidence


def test_term_in_text_is_word_bounded_for_short_terms():
    """Bare substring matching made 'AST' match 'gASTrointestinal' and the state code
    'MI' match 'MIami' — both produced confident, wrong clinical output."""
    assert not term_in_text("ast", "severe gastrointestinal disorders")
    assert term_in_text("ast", "elevated AST and ALT")
    assert not term_in_text("mi", "Mid Florida Hematology, Miami")
    assert term_in_text("michigan", "Detroit, Michigan, United States")


def test_grounded_source_accepts_verbatim_and_rejects_paraphrase():
    sources = {"title": "A Study of Drug X in HER2-positive Breast Cancer",
               "brief_summary": "Patients must have measurable disease."}
    assert grounded_source("HER2-positive Breast Cancer", sources) == "title"
    # Whitespace/case normalization is allowed; invention is not.
    assert grounded_source("her2-positive   breast cancer", sources) == "title"
    assert grounded_source("patients must have brain metastases", sources) is None
    assert grounded_source("", sources) is None


def test_deterministic_reasons_and_cautions_are_grounded_explanations(profile):
    rec = make_record()
    results = DeterministicReranker().rerank(profile, [make_candidate(rec)], 5)
    r = results[0]
    assert r.reasons and all(isinstance(x, Explanation) for x in r.reasons)
    sources = {"title": rec.title, "conditions": rec.condition_text,
               "brief_summary": rec.brief_summary, "status": rec.status,
               "interventions": rec.intervention_text,
               "locations": " | ".join(rec.locations)}
    for x in r.reasons + r.cautions:
        if not x.evidence_snippet or x.source_field in {"patient_profile", "study_design"}:
            continue
        # Every quote must be literally present in the field it claims to come from.
        assert grounded_source(x.evidence_snippet.strip("…"), sources) is not None, x
    assert r.evidence, "flattened evidence list should carry the grounded quotes"


def test_llm_reasons_without_verbatim_evidence_are_dropped():
    rec = make_record()
    kept, dropped = _ground_llm_items([
        {"text": "Targets the patient's HER2 alteration",
         "evidence": "HER2-positive Breast Cancer"},              # verbatim -> kept
        {"text": "Trial enrolls only patients with brain mets",
         "evidence": "requires untreated brain metastases"},      # fabricated -> dropped
        "Plain string with no evidence at all",                   # unverifiable -> dropped
    ], rec)
    assert [k.text for k in kept] == ["Targets the patient's HER2 alteration"]
    assert kept[0].source_field in {"title", "conditions"}
    assert kept[0].grounded is True
    assert dropped == 2


async def test_llm_path_keeps_deterministic_explanations_when_llm_is_ungrounded(profile):
    """An ungrounded explanation is worse than none: when every LLM claim fails the
    grounding check the card falls back to the rules-based (always-quoting) set."""
    class FakeClient:
        enabled = True

        async def complete_json(self, **kw):
            return {"trials": [{"nct": "NCT00000001", "confidence": 99,
                                "reasons": [{"text": "Great fit", "evidence": "not in this record at all"}],
                                "cautions": []}]}

    class FakeSettings:
        llm_rerank_model = "test/model"

    reranker = LLMReranker(FakeSettings(), client=FakeClient())
    results = await reranker.rerank(profile, [make_candidate(make_record())], 5)
    r = results[0]
    assert r.ungrounded_dropped == 1
    assert r.explained_by == "rules"           # LLM contributed nothing usable
    assert r.reasons and r.reasons[0].grounded  # deterministic explanations preserved


async def test_llm_grounded_reason_is_kept_with_its_source_field(profile):
    class FakeClient:
        enabled = True

        async def complete_json(self, **kw):
            return {"trials": [{"nct": "NCT00000001", "confidence": 88,
                                "reasons": [{"text": "HER2-directed therapy",
                                             "evidence": "HER2-positive Breast Cancer"}],
                                "cautions": []}]}

    class FakeSettings:
        llm_rerank_model = "test/model"

    results = await LLMReranker(FakeSettings(), client=FakeClient()).rerank(
        profile, [make_candidate(make_record())], 5)
    r = results[0]
    assert r.explained_by == "llm"
    assert r.reasons[0].evidence_snippet == "HER2-positive Breast Cancer"
    assert r.reasons[0].source_field in {"title", "conditions"}
    assert r.ungrounded_dropped == 0


# ------------------------------------------------- item 6: gates contribute to score

def test_disease_family_and_purpose_are_weighted_score_components(profile):
    """Both were reason-text only; the board was sorted by a number that ignored them."""
    gated = make_candidate(make_record(nct="NCT_GATED"))
    unclassified = make_candidate(
        make_record(nct="NCT_UNCLASSIFIED", conditions=["Neoplasms"],
                    title="A Study of Drug X in Advanced Neoplasms",
                    disease_families=frozenset()),
        disease_family="", disease_unclassified=True,
    )
    results = DeterministicReranker().rerank(profile, [gated, unclassified], 5)
    by_nct = {r.nct: r for r in results}
    assert by_nct["NCT_GATED"].breakdown.disease > 0
    assert by_nct["NCT_UNCLASSIFIED"].breakdown.disease == 0
    assert by_nct["NCT_GATED"].match_score > by_nct["NCT_UNCLASSIFIED"].match_score
    assert by_nct["NCT_GATED"].rank < by_nct["NCT_UNCLASSIFIED"].rank


def test_unknown_purpose_scores_below_a_stated_treatment_purpose(profile):
    treatment = make_candidate(make_record(nct="NCT_TREAT"))
    unlabeled = make_candidate(make_record(nct="NCT_UNLABELED", study_purpose="unknown"),
                               study_purpose="unknown", purpose_unverified=True)
    results = {r.nct: r for r in DeterministicReranker().rerank(profile, [treatment, unlabeled], 5)}
    assert results["NCT_TREAT"].breakdown.purpose > results["NCT_UNLABELED"].breakdown.purpose
    assert results["NCT_TREAT"].match_score > results["NCT_UNLABELED"].match_score


def test_unclassified_and_unverified_flags_reach_the_api_result(profile):
    c = make_candidate(make_record(disease_families=frozenset(), study_purpose="unknown"),
                       disease_family="", disease_unclassified=True,
                       study_purpose="unknown", purpose_unverified=True)
    r = DeterministicReranker().rerank(profile, [c], 1)[0]
    assert r.disease_unclassified is True and r.purpose_unverified is True
    texts = " ".join(x.text for x in r.cautions)
    assert "no recognized cancer family" in texts
    assert "no Primary Purpose" in texts


# ----------------------------------------------------------- item 1: geography in score

def test_location_match_boosts_and_miss_penalizes(profile):
    near = make_candidate(make_record(nct="NCT_NEAR"), location_query="Michigan",
                          matched_locations=["Karmanos Cancer Institute, Detroit, Michigan, 48201, United States"])
    far = make_candidate(make_record(nct="NCT_FAR", locations=["Some Center, Madrid, Spain"]),
                         location_query="Michigan", matched_locations=[])
    results = {r.nct: r for r in DeterministicReranker().rerank(profile, [near, far], 5)}
    assert results["NCT_NEAR"].breakdown.location > 0
    assert results["NCT_FAR"].breakdown.location < 0
    assert results["NCT_NEAR"].match_score > results["NCT_FAR"].match_score
    assert results["NCT_NEAR"].location_match is True
    # The miss must be VISIBLE, not silent.
    assert any("No listed study site in Michigan" in x.text for x in results["NCT_FAR"].cautions)


def test_no_location_requested_means_no_location_component(profile):
    r = DeterministicReranker().rerank(profile, [make_candidate(make_record())], 1)[0]
    assert r.breakdown.location == 0.0
    assert not any("study site" in x.text for x in r.cautions)


# ------------------------------------------------------- item 8: exclusion conflicts

def test_ecog_ceiling_parsing():
    assert ecog_ceiling("Patients with ECOG performance status of 0 or 1.")[0] == 1
    assert ecog_ceiling("ECOG PS 0-2 required.")[0] == 2
    assert ecog_ceiling("Subjects with ECOG performance status ≤ 1 are eligible.")[0] == 1
    assert ecog_ceiling("No performance status stated here.") is None


def test_patient_ecog_above_trial_ceiling_is_flagged_with_evidence():
    text = "Eligible patients must have an ECOG performance status of 0 or 1 at screening."
    conflicts = eligibility_conflicts(text, source_field="brief_summary",
                                      patient_conditions=[], ecog=2)
    assert len(conflicts) == 1
    assert conflicts[0].kind == "ecog"
    assert conflicts[0].snippet in text          # verbatim
    assert "ECOG 2" in conflicts[0].text
    # A patient at or below the ceiling is not flagged.
    assert not eligibility_conflicts(text, source_field="brief_summary",
                                     patient_conditions=[], ecog=1)


def test_patient_comorbidity_matching_trial_exclusion_is_flagged():
    """The requirements doc's canonical case, previously not implemented at all."""
    text = ("This study evaluates Drug X. Exclusion criteria include active hepatitis B "
            "infection, symptomatic brain metastases, or severe renal impairment.")
    conflicts = eligibility_conflicts(
        text, source_field="brief_summary",
        patient_conditions=["Hepatitis B carrier", "CKD stage III"], ecog=None)
    kinds = {c.kind for c in conflicts}
    assert kinds == {"exclusion"}
    flagged = " ".join(c.text for c in conflicts)
    assert "Hepatitis B carrier" in flagged and "CKD stage III" in flagged
    for c in conflicts:
        assert c.snippet and c.snippet in text.replace("  ", " ")


def test_organ_function_flag_checked_against_exclusions():
    text = "Patients are excluded for hepatic impairment or elevated bilirubin at baseline."
    conflicts = eligibility_conflicts(text, source_field="brief_summary",
                                      patient_conditions=["LFT elevation"], ecog=None)
    assert conflicts and conflicts[0].kind == "exclusion"


def test_exclusion_check_does_not_fire_on_unrelated_prose():
    """Precision guard: 'excluding cardiac surgery' is not an exclusion of the
    patient's cardiac failure, and 'gastrointestinal' is not an AST elevation."""
    text = ("Patients scheduled for elective surgical procedures (excluding cardiac surgery). "
            "Exclusion criteria include severe gastrointestinal disorders.")
    assert not eligibility_conflicts(text, source_field="brief_summary",
                                     patient_conditions=["cardiac failure"], ecog=None)
    assert not eligibility_conflicts(text, source_field="brief_summary",
                                     patient_conditions=["LFT elevation"], ecog=None)


def test_eligibility_conflicts_surface_as_cautions_with_evidence(profile):
    text = ("Drug X trial. Exclusion criteria include active hepatitis B infection.")
    rec = make_record(summary=text)
    profile.comorbidities = ["hepatitis B"]
    conflicts = eligibility_conflicts(text, source_field="brief_summary",
                                      patient_conditions=profile.comorbidities, ecog=None)
    r = DeterministicReranker().rerank(
        profile, [make_candidate(rec, eligibility_conflicts=conflicts)], 1)[0]
    hit = next(x for x in r.cautions if "hepatitis B" in x.text)
    assert hit.source_field == "brief_summary"
    assert hit.evidence_snippet in text


# ------------------------------------------ item 5 + API wiring: filters reach retrieval

def test_match_request_maps_every_filter_to_retrieval_filters():
    """`location` was accepted by the API and never reached retrieval. This mapping is
    the single choke point that makes that class of bug visible."""
    from app.api.schemas import MatchRequest

    req = MatchRequest(deidentified_text="62F HER2-positive breast cancer", top_k=5,
                       active_only=True, recruiting_only=True, interventional_only=True,
                       treatment_only=True, location="Detroit, Michigan",
                       location_required=True)
    f = req.to_retrieval_filters()
    assert f.recruiting_only is True
    assert f.location == "Detroit, Michigan"
    assert f.location_required is True
    assert f.active_only is True and f.treatment_only is True


def test_recruiting_only_defaults_off_and_is_distinct_from_active_only():
    from app.api.schemas import MatchRequest
    from app.trials.retrieve import RetrievalFilters

    assert MatchRequest(deidentified_text="x").recruiting_only is False
    assert RetrievalFilters().recruiting_only is False
    assert RetrievalFilters().active_only is True


def test_active_not_recruiting_passes_active_but_not_recruiting_filter():
    from app.trials.retrieve import _passes_hard_filters

    rec = make_record(status="ACTIVE_NOT_RECRUITING")
    profile = PatientProfile()
    from app.trials.retrieve import RetrievalFilters
    assert _passes_hard_filters(rec, profile, RetrievalFilters(active_only=True,
                                                               interventional_only=False))
    assert not _passes_hard_filters(rec, profile, RetrievalFilters(active_only=True,
                                                                   recruiting_only=True,
                                                                   interventional_only=False))


# --------------------------------------------------- item 1: the geography notice

def test_location_notice_states_when_geography_could_not_be_honoured():
    from app.matching.pipeline import _location_notice
    from app.trials.retrieve import RetrievalFilters

    f = RetrievalFilters(location="Alaska")
    assert "No listed study site in Alaska" in _location_notice("Alaska", 0, 10, f)
    assert "3 of 10" in _location_notice("Alaska", 3, 10, f)
    assert _location_notice("Alaska", 10, 10, f) is None
    assert _location_notice("", 0, 10, f) is None
