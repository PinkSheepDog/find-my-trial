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


# ---------------- context-window clamping (panel-line realism) ----------------

def test_negation_survives_preceding_separators_in_a_panel_line():
    """_window pulls its left edge to the last clause break before the marker. The
    offsets were added to an already-mutated `left`, so with several separators before
    the marker the edge overshot PAST it and truncated the negation:

        "...biopsy); ALK No rearrangement detected (2022-02-14, ..."
                  clamped to ->  "ement detected (2022-02-14, "

    losing the "No", so the trailing "detected" classified as POSITIVE. Isolated
    one-clause tests could never catch this; real pathology panels always look like
    the strings below."""
    cases = [
        ("Panel (2022-02-14, right lung core biopsy); ALK No rearrangement detected "
         "(2022-02-14, right lung core biopsy).", "ALK"),
        ("NGS panel, blood specimen, 2024-01-02; BRCA1 no pathogenic variant detected "
         "(germline, 2024-01-02).", "BRCA"),
        ("Results, reported 2023-05-01, reviewed; ROS1 no fusion identified "
         "(tissue, 2023-05-01).", "ROS1"),
        ("Molecular, foundation one, 2021-11-03; EGFR no activating mutation detected "
         "(plasma, 2021-11-03).", "EGFR"),
    ]
    for text, marker in cases:
        status = _status(extract(text), marker)
        assert status == BiomarkerStatus.NEGATIVE, f"{marker} in {text!r} -> {status}"


def test_window_left_edge_never_passes_the_marker():
    """Directly pin the clamping invariant: whatever separators precede a marker, the
    window must still contain the marker itself."""
    import re
    from app.extraction.rules_extractor import _BIOMARKERS
    ex = RulesExtractor()
    text = ("history, prior therapy, imaging 2020-01-01; note, addendum, "
            "reviewed; ALK no rearrangement detected.").lower()
    m = re.search(_BIOMARKERS["ALK"], text)
    window = ex._window(text, m.start(), m.end(), radius=40)
    assert "alk" in window, f"window lost the marker: {window!r}"
    assert "no rearrangement detected" in window, f"window lost the negation: {window!r}"


# ------------- contradictory mentions must resolve conservatively -------------

def test_conflicting_mentions_never_resolve_to_positive():
    """Copied-forward problem lists routinely contradict the current specimen. Keeping
    whichever mention appeared FIRST let a stale "ER positive" outrank a current biopsy's
    "ER negative", matching an ER-negative patient to endocrine-therapy trials."""
    p = extract("Breast cancer, Stage IIIA, ER positive. "
                "ER <1% on the current liver biopsy, ER negative.")
    assert _status(p, "ER") == BiomarkerStatus.NEGATIVE
    assert p.biomarker("ER").certainty.value == "uncertain", "conflict must be flagged for review"


def test_conflict_resolution_is_order_independent():
    """The conservative reading must win regardless of which mention is written first."""
    a = extract("HER2 positive on the 2019 primary. HER2 negative on the current biopsy.")
    b = extract("HER2 negative on the current biopsy. HER2 positive on the 2019 primary.")
    assert _status(a, "HER2") == _status(b, "HER2") != BiomarkerStatus.POSITIVE


def test_conflict_does_not_downgrade_an_unconflicted_positive():
    """Conservative resolution must not fire when there is no actual contradiction."""
    p = extract("Metastatic breast cancer. HER2 IHC 3+, HER2 amplified by FISH.")
    assert _status(p, "HER2") == BiomarkerStatus.POSITIVE


def test_historical_and_current_are_kept_as_separate_records():
    """Conversion between timepoints is clinically meaningful and must not be collapsed."""
    p = extract("HER2 IHC 3+ amplified on the 2019 primary specimen (historical). "
                "HER2 IHC 0, ISH not amplified on the current liver biopsy.")
    her2 = [b for b in p.biomarkers if b.name == "HER2"]
    assert len(her2) == 2, [(b.status.value, b.timing.value) for b in her2]
    assert {b.timing.value for b in her2} == {"historical", "current"}
