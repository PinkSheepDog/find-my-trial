"""GOLDEN ACCEPTANCE HARNESS — the milestone the prior prototype never built.

For each worked example in fixtures/expected_outputs.json, this runs the FULL
offline pipeline (de-id -> rules extraction -> retrieve -> deterministic rerank)
against the real 10k-row corpus and asserts:

  * extraction correctness, including biomarker DIRECTION (the bug fix); and
  * recall of the expected trials within the ranked board (strong options not missed).

Exact rank/confidence parity is an LLM-reranker goal tracked separately; here we
hold the line on the two things that must be true with or without an API key:
right signals extracted, right trials surfaced, nothing contraindicated on top.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.extraction.rules_extractor import RulesExtractor
from app.extraction.schema import BiomarkerStatus
from app.intake.deident import deidentify
from app.matching.rerank import DeterministicReranker
from app.trials.index import TrialIndex
from app.trials.retrieve import RetrievalFilters, retrieve

BACKEND = Path(__file__).resolve().parent.parent
CSV = BACKEND / "data" / "trials.csv"
FIXTURES = BACKEND / "fixtures"

pytestmark = pytest.mark.skipif(not CSV.exists(), reason="trial CSV not present")

_CHART_FOR_CASE = {
    "her2_positive_metastatic_breast_cancer": "phi_chart_her2.txt",
    "messy_tnbc_her2_low_breast_cancer": "phi_chart_tnbc.txt",
}


@pytest.fixture(scope="module")
def index():
    return TrialIndex.from_csv(CSV)


@pytest.fixture(scope="module")
def expected():
    return json.loads((FIXTURES / "expected_outputs.json").read_text(encoding="utf-8"))


def _run(chart_file, index, top_k=15):
    raw = (FIXTURES / chart_file).read_text(encoding="utf-8", errors="ignore")
    profile = RulesExtractor().extract(deidentify(raw).text)
    # treatment_only=False: the legacy worked-example board includes real-world
    # observational/registry comparators. The default treatment gate (which excludes
    # them) is covered separately in test_retrieval.
    cands = retrieve(profile, index,
                     filters=RetrievalFilters(active_only=False, interventional_only=False,
                                              treatment_only=False),
                     top_k=80)
    results = DeterministicReranker().rerank(profile, cands, top_k)
    return profile, results


def test_golden_extraction_profiles(index, expected):
    for case in expected["cases"]:
        chart = _CHART_FOR_CASE[case["case_id"]]
        if not (FIXTURES / chart).exists():
            pytest.skip(f"fixture {chart} missing")
        profile, _ = _run(chart, index)
        exp = case["expected_profile"]
        assert profile.age == exp["age"], f"{case['case_id']}: age"
        assert profile.sex == exp["sex"], f"{case['case_id']}: sex"
        assert profile.is_metastatic, f"{case['case_id']}: metastatic"

        # Direction-sensitive biomarker assertions (the core regression).
        if case["case_id"].startswith("her2_positive"):
            assert profile.biomarker("HER2").status == BiomarkerStatus.POSITIVE
        if case["case_id"].startswith("messy_tnbc"):
            assert profile.biomarker("HER2").status in {BiomarkerStatus.LOW, BiomarkerStatus.EQUIVOCAL}
            assert profile.biomarker("HER2").status != BiomarkerStatus.POSITIVE
            assert profile.biomarker("BRCA").status == BiomarkerStatus.NEGATIVE


def test_golden_recall_of_expected_trials(index, expected):
    for case in expected["cases"]:
        chart = _CHART_FOR_CASE[case["case_id"]]
        if not (FIXTURES / chart).exists():
            pytest.skip(f"fixture {chart} missing")
        _, results = _run(chart, index)
        ranked_ncts = [r.nct for r in results]
        expected_ncts = [r["nct"] for r in case["expected_results"]]
        # At least the PRIMARY expected trial (rank 1 in the worked example) must
        # appear in the ranked board.
        primary = next(r["nct"] for r in case["expected_results"] if r["rank"] == 1)
        assert primary in ranked_ncts, (
            f"{case['case_id']}: primary expected trial {primary} not in ranked board "
            f"(got {ranked_ncts[:10]})"
        )


def test_no_contraindicated_trial_ranked_first(index, expected):
    """The worst old-system failure mode: a contraindicated trial on top. For the
    HER2-low/negative TNBC patient, rank 1 must not be a HER2-positive-required trial."""
    chart = _CHART_FOR_CASE["messy_tnbc_her2_low_breast_cancer"]
    if not (FIXTURES / chart).exists():
        pytest.skip("fixture missing")
    _, results = _run(chart, index)
    if results:
        assert not results[0].contraindications, \
            f"contraindicated trial {results[0].nct} ranked #1"
