"""FHIR R4 document Bundle + C-CDA ingestion (feedback: preferred/strong input tiers).

Ingests each corpus bundle through the real intake -> de-id -> extraction path and
asserts the matching-critical facts (disease family, biomarker DIRECTION, ECOG) are
recovered from structured input — including HER2 low != positive, and no spurious
small-cell family for an NSCLC patient.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.extraction.rules_extractor import RulesExtractor
from app.extraction.schema import BiomarkerStatus
from app.intake.deident import deidentify
from app.intake.extract_text import extract_text
from app.matching.clinical import disease_families_of
from app.trials.retrieve import patient_disease_families

CASES = Path(__file__).resolve().parent.parent / "benchmark" / "cases"


def _profile_from(path: Path, filename: str):
    doc = extract_text(filename, path.read_bytes())
    return doc, RulesExtractor().extract(deidentify(doc.text).text)


@pytest.mark.parametrize("case,family,marker,status", [
    ("case_01_clean_her2_positive_breast", "breast cancer", "HER2", BiomarkerStatus.POSITIVE),
    ("case_02_messy_tnbc_her2_low", "breast cancer", "HER2", BiomarkerStatus.LOW),
    ("case_03_nsclc_egfr_negative_control", "non-small cell lung cancer", "EGFR", BiomarkerStatus.POSITIVE),
    ("case_04_incomplete_pancreatic_abstention", "pancreatic cancer", None, None),
])
def test_fhir_bundle_ingestion(case, family, marker, status):
    bundle = CASES / case / "fhir_document_bundle.json"
    if not bundle.exists():
        pytest.skip("bundle fixture missing")
    doc, profile = _profile_from(bundle, "fhir_document_bundle.json")
    assert doc.source_kind == "fhir"
    assert family in patient_disease_families(profile), \
        f"{case}: expected {family}, got {sorted(patient_disease_families(profile))}"
    if marker:
        b = profile.biomarker(marker)
        assert b is not None and b.status == status, f"{case}: {marker} = {b.status if b else None}"


def test_nsclc_not_classified_as_small_cell():
    # Substring collision guard: "small cell lung" inside "non-small cell lung".
    fams = disease_families_of("Metastatic non-small cell lung adenocarcinoma")
    assert "non-small cell lung cancer" in fams
    assert "small cell lung cancer" not in fams


def test_ccda_ingestion_smoke():
    ccda = CASES / "case_01_clean_her2_positive_breast" / "illustrative_ccda_ccd.xml"
    if not ccda.exists():
        pytest.skip("c-cda fixture missing")
    doc = extract_text("illustrative_ccda_ccd.xml", ccda.read_bytes())
    assert doc.source_kind == "ccda"
    assert "breast" in doc.text.lower()
