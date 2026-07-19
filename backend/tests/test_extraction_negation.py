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


# ------------- phrase-level negation (noun phrase splits the negator) -------------
# The cue list is literal substrings, so it could only ever match negations where the
# negator and verb are adjacent ("not detected"). Standard pathology phrasing puts a
# noun between them ("no rearrangement detected"), which read as POSITIVE off the
# trailing "detected" — the original negative-read-as-positive bug in new clothing.
# Every case below FAILED before the _NEG_PATTERNS fix.

@pytest.mark.parametrize("text,marker", [
    ("ALK No rearrangement detected.", "ALK"),
    ("No ALK rearrangement detected.", "ALK"),
    ("ALK: no rearrangement identified.", "ALK"),
    ("ROS1 no fusion detected.", "ROS1"),
    ("ROS1 no fusion identified on NGS.", "ROS1"),
    ("BRCA1 no pathogenic variant detected.", "BRCA"),
    ("BRCA2 no deleterious mutation found.", "BRCA"),
    ("EGFR no activating mutation detected.", "EGFR"),
    ("KRAS no mutation seen.", "KRAS"),
    ("BRAF no variant observed.", "BRAF"),
    ("EGFR without evidence of mutation.", "EGFR"),
    ("ALK negative for rearrangement.", "ALK"),
    ("ROS1 no evidence of fusion.", "ROS1"),
])
def test_phrase_negation_is_never_positive(text, marker):
    status = _status(extract(text), marker)
    assert status == BiomarkerStatus.NEGATIVE, f"{text!r} -> {status}"
    assert status != BiomarkerStatus.POSITIVE


@pytest.mark.parametrize("text,marker", [
    ("ALK rearrangement detected.", "ALK"),
    ("EGFR Exon 19 deletion detected, activating mutation.", "EGFR"),
    ("BRAF V600E mutation present.", "BRAF"),
    ("KRAS G12C mutated.", "KRAS"),
])
def test_phrase_negation_does_not_suppress_true_positives(text, marker):
    """The negation patterns must not over-fire and flip real positives."""
    assert _status(extract(text), marker) == BiomarkerStatus.POSITIVE


def test_negation_does_not_leak_across_clauses():
    p = extract("ALK no rearrangement detected. BRAF V600E mutation detected.")
    assert _status(p, "ALK") == BiomarkerStatus.NEGATIVE
    assert _status(p, "BRAF") == BiomarkerStatus.POSITIVE


def test_her2_negated_amplification_is_not_positive():
    p = extract("Breast cancer, HER2 no amplification detected by FISH.")
    assert _status(p, "HER2") != BiomarkerStatus.POSITIVE


# ------------------------ disease-family disambiguation ------------------------

def test_nsclc_does_not_also_extract_sclc():
    """'small cell lung' is a substring of 'non-small cell lung'. Because the disease
    gate admits any trial sharing a family, a bare match lets SCLC trials clear the
    gate for an NSCLC patient — two diseases with non-overlapping treatment."""
    p = extract("Patient has metastatic non-small cell lung cancer, adenocarcinoma.")
    assert "Non-Small Cell Lung Cancer" in p.cancer_types
    assert "Small Cell Lung Cancer" not in p.cancer_types


def test_sclc_still_extracts_when_genuinely_present():
    p = extract("Patient has extensive-stage small cell lung cancer.")
    assert "Small Cell Lung Cancer" in p.cancer_types
    assert "Non-Small Cell Lung Cancer" not in p.cancer_types


def test_sclc_abbreviation_still_matches():
    assert "Small Cell Lung Cancer" in extract("Dx: SCLC, extensive stage.").cancer_types
