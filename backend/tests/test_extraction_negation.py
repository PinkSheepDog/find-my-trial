"""Regression tests for the single most dangerous defect in the prior prototype:
biomarker negation/direction being lost, so "BRCA negative" and "HER2 IHC 1+"
were read as POSITIVE and the patient was matched to contraindicated trials.

These tests pin the fix: direction is always represented and correct.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.extraction.rules_extractor import RulesExtractor
from app.extraction.schema import BiomarkerStatus
from app.intake.deident import deidentify

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
extract = RulesExtractor().extract


def _status(profile, name):
    b = profile.biomarker(name)
    return b.status if b else None


# --------------------------- the exact bug cases ---------------------------

def test_her2_ihc_1plus_is_LOW_not_positive():
    p = extract("Patient with metastatic breast cancer, HER2 IHC 1+, FISH not amplified.")
    assert _status(p, "HER2") == BiomarkerStatus.LOW
    assert _status(p, "HER2") != BiomarkerStatus.POSITIVE


def test_her2_positive_is_positive():
    p = extract("Metastatic HER2-positive breast cancer, IHC 3+.")
    assert _status(p, "HER2") == BiomarkerStatus.POSITIVE


def test_brca_negative_is_negative():
    p = extract("Germline BRCA negative. PD-L1 positive.")
    assert _status(p, "BRCA") == BiomarkerStatus.NEGATIVE
    assert _status(p, "PD-L1") == BiomarkerStatus.POSITIVE


def test_fish_not_amplified_is_not_positive():
    p = extract("HER2 FISH ratio 1.3, not amplified.")
    assert _status(p, "HER2") in {BiomarkerStatus.LOW, BiomarkerStatus.NEGATIVE}
    assert _status(p, "HER2") != BiomarkerStatus.POSITIVE


def test_msi_stable_is_negative_not_positive():
    p = extract("MSI stable. HRD borderline.")
    assert _status(p, "MSI") == BiomarkerStatus.NEGATIVE


def test_tnbc_infers_her2_negative():
    p = extract("64F metastatic triple-negative breast cancer.")
    # TNBC must not surface as HER2-positive; inferred-negative is acceptable.
    assert _status(p, "HER2") in {BiomarkerStatus.NEGATIVE, None}
    assert _status(p, "HER2") != BiomarkerStatus.POSITIVE


# --------------------------- end-to-end on the messy TNBC chart ---------------------------

def test_messy_tnbc_chart_extraction():
    path = FIXTURES / "phi_chart_tnbc.txt"
    if not path.exists():
        pytest.skip("PHI fixture not present")
    raw = path.read_text(encoding="utf-8", errors="ignore")
    deid = deidentify(raw).text
    p = extract(deid)

    # The headline safety property: this patient is HER2-LOW and BRCA-NEGATIVE.
    assert _status(p, "HER2") in {BiomarkerStatus.LOW, BiomarkerStatus.EQUIVOCAL}, \
        f"HER2 misclassified as {_status(p, 'HER2')}"
    assert _status(p, "HER2") != BiomarkerStatus.POSITIVE
    assert _status(p, "BRCA") == BiomarkerStatus.NEGATIVE
    assert _status(p, "PD-L1") == BiomarkerStatus.POSITIVE

    # Core demographics / status survived the messy copy-forward noise.
    assert p.age == 64
    assert p.sex == "Female"
    assert p.ecog == 2
    assert p.is_metastatic
    assert "Triple-Negative Breast Cancer" in p.cancer_types

    # Prior atezolizumab toxicity must be captured for the checkpoint-exclusion caution.
    atezo = next((t for t in p.therapies if t.name == "Atezolizumab"), None)
    assert atezo is not None
    assert atezo.caused_toxicity is not None


def test_her2_positive_chart_extraction():
    path = FIXTURES / "phi_chart_her2.txt"
    if not path.exists():
        pytest.skip("PHI fixture not present")
    raw = path.read_text(encoding="utf-8", errors="ignore")
    p = extract(deidentify(raw).text)

    assert _status(p, "HER2") == BiomarkerStatus.POSITIVE
    assert p.age == 62
    assert p.sex == "Female"
    assert p.ecog == 1
    assert p.is_metastatic
    names = p.therapy_names()
    assert "Trastuzumab" in names
    assert "Pertuzumab" in names
