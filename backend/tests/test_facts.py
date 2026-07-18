"""Reviewable per-fact provenance + the six review states (feedback P1)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.extraction.rules_extractor import RulesExtractor
from app.extraction.schema import ReviewState, derive_facts
from app.intake.deident import deidentify
from app.intake.extract_text import extract_text

CASES = Path(__file__).resolve().parent.parent / "benchmark" / "cases"


def _facts(text: str):
    p = RulesExtractor().extract(deidentify(text).text)
    return derive_facts(p)


def _facts_from_bundle(case: str):
    b = CASES / case / "fhir_document_bundle.json"
    if not b.exists():
        pytest.skip("bundle fixture missing")
    p = RulesExtractor().extract(deidentify(extract_text("b.json", b.read_bytes()).text).text)
    return derive_facts(p)


def test_narrative_states():
    facts = _facts("64F metastatic TNBC, BRCA negative, ECOG 1, prior trastuzumab.")
    states = {f.review_state for f in facts}
    assert ReviewState.CONFIRMED in states          # stage / ecog / therapy
    assert ReviewState.INFERRED in states           # TNBC -> ER/PR/HER2 negative
    assert ReviewState.NEGATED in states            # BRCA negative


def test_fhir_case02_historical_and_evidence():
    facts = _facts_from_bundle("case_02_messy_tnbc_her2_low")
    assert any(f.review_state == ReviewState.HISTORICAL and f.fact_type == "biomarker.HER2" for f in facts)
    bio = [f for f in facts if f.fact_type.startswith("biomarker.")]
    assert bio and any(f.evidence for f in bio), "biomarker facts must carry source evidence"


def test_case04_surfaces_missing():
    facts = _facts_from_bundle("case_04_incomplete_pancreatic_abstention")
    assert any(f.review_state == ReviewState.MISSING for f in facts)
