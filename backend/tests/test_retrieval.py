"""Retrieval recall against the REAL 10k-row trial corpus.

This is the test the prior prototype never had: run the golden patient charts
through de-id -> extraction -> retrieval and assert the expected trials actually
surface near the top. We assert recall@k (the strong options aren't missed), not
exact ranks — exact ordering is the LLM reranker's job and is checked separately.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.extraction.rules_extractor import RulesExtractor
from app.intake.deident import deidentify
from app.trials.index import TrialIndex
from app.trials.retrieve import RetrievalFilters, retrieve

BACKEND = Path(__file__).resolve().parent.parent
CSV = BACKEND / "data" / "trials.csv"
FIXTURES = BACKEND / "fixtures"

pytestmark = pytest.mark.skipif(not CSV.exists(), reason="trial CSV not present locally")


@pytest.fixture(scope="module")
def index() -> TrialIndex:
    return TrialIndex.from_csv(CSV)


def _profile(fixture: str):
    raw = (FIXTURES / fixture).read_text(encoding="utf-8", errors="ignore")
    return RulesExtractor().extract(deidentify(raw).text)


def test_index_loads_sample_corpus(index):
    # data/trials.csv is a 10,000-row SAMPLE of ClinicalTrials.gov, not the full
    # registry (~555,508 studies) — roughly 1.8% of it. Recall numbers in this file
    # are therefore sample recall and do not generalize to the full registry.
    assert index.stats()["trial_count"] == 10000


def test_her2_patient_surfaces_expected_trials(index):
    if not (FIXTURES / "phi_chart_her2.txt").exists():
        pytest.skip("fixture missing")
    profile = _profile("phi_chart_her2.txt")
    # The worked example's primary comparator (NCT05253911) is a real-world OBSERVATIONAL
    # study. Recall check runs with the treatment gate OFF so the disease-family gate and
    # BM25 retrieval are exercised without the purpose gate hiding the comparator.
    cands = retrieve(profile, index, filters=RetrievalFilters(
        active_only=False, interventional_only=False, treatment_only=False), top_k=60)
    ncts = {c.record.nct for c in cands}
    assert "NCT05253911" in ncts, "primary HER2 trial NCT05253911 not retrieved"


def test_treatment_gate_excludes_observational(index):
    """Feedback P0: a treatment query must NOT return registry/observational studies.
    NCT05253911 is OBSERVATIONAL, so the default treatment gate must drop it."""
    if not (FIXTURES / "phi_chart_her2.txt").exists():
        pytest.skip("fixture missing")
    profile = _profile("phi_chart_her2.txt")
    cands = retrieve(profile, index, filters=RetrievalFilters(
        active_only=False, interventional_only=False, treatment_only=True), top_k=60)
    ncts = {c.record.nct for c in cands}
    assert "NCT05253911" not in ncts, "observational study leaked into a treatment query"


def test_tnbc_patient_surfaces_expected_trials(index):
    if not (FIXTURES / "phi_chart_tnbc.txt").exists():
        pytest.skip("fixture missing")
    profile = _profile("phi_chart_tnbc.txt")
    cands = retrieve(profile, index, filters=RetrievalFilters(active_only=False,
                     interventional_only=False), top_k=80)
    ncts = {c.record.nct for c in cands}
    expected = {"NCT06371274", "NCT01898117", "NCT01251874"}
    hits = expected & ncts
    assert hits, f"none of the expected TNBC trials retrieved; got {len(ncts)} candidates"


def test_contraindication_flagged_for_her2_negative_patient(index):
    """A HER2-low/negative TNBC patient must have any HER2-positive-required trial
    flagged as a contraindication — never silently boosted (the old bug)."""
    profile = _profile("phi_chart_tnbc.txt") if (FIXTURES / "phi_chart_tnbc.txt").exists() else None
    if profile is None:
        pytest.skip("fixture missing")
    cands = retrieve(profile, index, filters=RetrievalFilters(active_only=False,
                     interventional_only=False), top_k=200)
    flagged = [c for c in cands if c.contraindications]
    # Either there are no HER2-positive-required trials in range, or they are flagged
    # (not silently ranked high). We assert the mechanism produced no false "match".
    for c in cands[:10]:
        assert not c.contraindications, \
            f"contraindicated trial {c.record.nct} ranked in top 10 without demotion"


# ---------------------------------------------------------------------------
# Audit fixes, exercised against the REAL corpus. The `fixtures/phi_chart_*.txt`
# charts are not in the repo, so these use the benchmark case notes, which are.
# ---------------------------------------------------------------------------

BENCH_CASES = BACKEND / "benchmark" / "cases"


def _bench_profile(case: str):
    note = (BENCH_CASES / case / "source_note.txt").read_text(encoding="utf-8", errors="ignore")
    return RulesExtractor().extract(deidentify(note).text)


@pytest.fixture(scope="module")
def her2_profile():
    return _bench_profile("case_01_clean_her2_positive_breast")


def _filters(**kw) -> RetrievalFilters:
    base = dict(active_only=False, interventional_only=False, treatment_only=True)
    base.update(kw)
    return RetrievalFilters(**base)


def test_location_filter_actually_affects_results(index, her2_profile):
    """The location filter was populated from the API and NEVER READ: a clinician
    filtering to their state got unfiltered results with no warning."""
    plain = retrieve(her2_profile, index, filters=_filters(), top_k=20)
    michigan = retrieve(her2_profile, index, filters=_filters(location="Michigan"), top_k=20)
    assert [c.record.nct for c in plain] != [c.record.nct for c in michigan], \
        "location filter changed nothing — it is still dead"
    matched = [c for c in michigan if c.matched_locations]
    assert matched, "no candidate matched a Michigan site"
    for c in matched:
        assert any("michigan" in s.lower() for s in c.matched_locations)
    # A matching site is a strong boost: matched candidates rank above their own
    # unboosted position, and the top of the board is dominated by them.
    assert any(c.matched_locations for c in michigan[:3])


def test_location_state_abbreviation_resolves_to_the_state(index, her2_profile):
    """'MI' must mean Michigan — not a substring match on Miami / Memorial / Mid-."""
    cands = retrieve(her2_profile, index,
                     filters=_filters(location="MI", location_required=True), top_k=20)
    assert cands, "state abbreviation matched nothing"
    for c in cands:
        assert all("michigan" in s.lower() for s in c.matched_locations), c.matched_locations


def test_location_required_is_a_hard_filter(index, her2_profile):
    cands = retrieve(her2_profile, index,
                     filters=_filters(location="Michigan", location_required=True), top_k=30)
    assert cands
    for c in cands:
        assert c.matched_locations, f"{c.record.nct} has no Michigan site but survived a hard filter"


def test_location_preferences_from_the_chart_are_used_when_no_filter_given(index, her2_profile):
    profile = her2_profile.model_copy(deep=True)
    profile.location_preferences = ["Michigan"]
    cands = retrieve(profile, index, filters=_filters(), top_k=20)
    assert any(c.matched_locations for c in cands)
    assert all(c.location_query == "Michigan" for c in cands)


def test_recruiting_only_excludes_active_not_recruiting(index, her2_profile):
    """'Active only' includes ACTIVE_NOT_RECRUITING — a study running but CLOSED to
    new patients. `recruiting_only` is the honest filter."""
    active = retrieve(her2_profile, index, filters=_filters(active_only=True), top_k=200)
    recruiting = retrieve(her2_profile, index,
                          filters=_filters(active_only=True, recruiting_only=True), top_k=200)
    active_statuses = {c.record.status for c in active}
    assert "ACTIVE_NOT_RECRUITING" in active_statuses, "corpus sample lacks the case under test"
    assert "ACTIVE_NOT_RECRUITING" not in {c.record.status for c in recruiting}
    assert all(c.record.is_recruiting for c in recruiting)
    assert recruiting, "recruiting-only filter emptied the board"


def test_non_oncology_trials_are_rejected_for_a_cancer_patient(index, her2_profile):
    """The disease gate only ran when the trial had a recognized family, so a trial
    with none (81% of this corpus) skipped it and competed on BM25 alone."""
    from app.matching.clinical import is_oncology_text

    cands = retrieve(her2_profile, index, filters=_filters(), top_k=200)
    unfamilied = [c for c in cands if not c.record.disease_families]
    for c in unfamilied:
        rec = c.record
        blob = " ".join([rec.title, rec.condition_text, rec.brief_summary])
        # Surviving without a recognized family now requires at least cancer language
        # (and is flagged + demoted); a schizophrenia or bioequivalence study is gone.
        assert is_oncology_text(blob), f"non-oncology trial {rec.nct} on a cancer board: {rec.title[:70]}"
        assert c.disease_unclassified or c.basket_evidence or c.matched_biomarkers


def test_unclassified_trials_are_flagged_not_exempted(index, her2_profile):
    cands = retrieve(her2_profile, index, filters=_filters(), top_k=200)
    for c in cands:
        if c.disease_unclassified:
            assert not c.record.disease_families
            assert not c.disease_family
    # A trial that never faced the gate must not lead the board.
    assert not any(c.disease_unclassified for c in cands[:5])


def test_treatment_gate_uses_the_refined_purpose(index, her2_profile):
    from app.matching.clinical import NON_TREATMENT_PURPOSES

    cands = retrieve(her2_profile, index, filters=_filters(treatment_only=True), top_k=200)
    leaked = [(c.record.nct, c.study_purpose) for c in cands
              if c.study_purpose in NON_TREATMENT_PURPOSES]
    assert not leaked, f"non-treatment studies cleared the treatment gate: {leaked[:5]}"


def test_ecog_ceiling_is_detected_in_real_registry_text(index):
    """The exclusion-criteria checker must work on real corpus prose, not just fixtures."""
    from app.matching.clinical import ecog_ceiling, eligibility_conflicts

    with_ceiling = [r for r in index.records if ecog_ceiling(r.brief_summary)]
    assert with_ceiling, "no corpus record states an ECOG ceiling"
    rec = with_ceiling[0]
    ceiling, _ = ecog_ceiling(rec.brief_summary)
    conflicts = eligibility_conflicts(rec.brief_summary, source_field="brief_summary",
                                      patient_conditions=[], ecog=ceiling + 1)
    assert conflicts and conflicts[0].kind == "ecog"
    assert conflicts[0].snippet in " ".join(rec.brief_summary.split())
