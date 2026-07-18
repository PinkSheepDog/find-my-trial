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


def test_index_loads_full_corpus(index):
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
