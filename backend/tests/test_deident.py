"""De-identification is the HIPAA safety boundary. These tests assert two things:
  1. Identifiers are removed (names, MRN, DOB, dates, phone, email, addresses, IDs).
  2. Clinical signal SURVIVES (biomarkers, drugs, ECOG, stage, lab values) — over-
     redaction that destroys medicine is a bug, not safety.

The real (synthetic) PHI charts live in fixtures/ and are gitignored. If they are
absent (e.g. clean checkout), the fixture-based tests skip; the unit tests always run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.intake.deident import (
    TAG_ADDRESS,
    TAG_DATE,
    TAG_EMAIL,
    TAG_MRN,
    TAG_NAME,
    deidentify,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# --------------------------- unit-level identifier removal ---------------------------

def test_removes_email():
    r = deidentify("Contact patient at jane.doe@example.com for follow-up.")
    assert "jane.doe@example.com" not in r.text
    assert TAG_EMAIL in r.text


def test_removes_phone_and_ssn():
    r = deidentify("Call (313) 555-0142. SSN 123-45-6789.")
    assert "555-0142" not in r.text
    assert "123-45-6789" not in r.text


def test_removes_mrn():
    r = deidentify("Patient MRN 0048239 seen today.")
    assert "0048239" not in r.text
    assert TAG_MRN in r.text


def test_removes_labeled_and_titled_names():
    r = deidentify("Patient Name: Maria E. Thompson. Seen by Dr. Patel.")
    assert "Maria" not in r.text
    assert "Thompson" not in r.text
    assert "Patel" not in r.text
    assert TAG_NAME in r.text


def test_removes_dob_and_dates():
    r = deidentify("DOB 09/25/1961. Started chemo March 2014. Biopsy 4-12-2019.")
    assert "1961" not in r.text
    assert "March 2014" not in r.text
    assert "4-12-2019" not in r.text
    assert TAG_DATE in r.text


def test_removes_city_state_address():
    r = deidentify("Patient from Detroit, MI prefers local sites.")
    assert "Detroit, MI" not in r.text
    assert TAG_ADDRESS in r.text


def test_removes_patient_record_id():
    r = deidentify("Patient ID: P-1001 active.")
    assert "P-1001" not in r.text


def test_generalizes_age_over_89_but_keeps_age_in_years():
    over = deidentify("The patient is a 92 year old female.")
    assert "92" not in over.text
    keep = deidentify("The patient is a 64 year old female.")
    assert "64" in keep.text  # ages <= 89 retained for trial age-eligibility matching


# --------------------------- clinical signal must SURVIVE ---------------------------

def test_clinical_signal_survives():
    chart = (
        "64F metastatic TNBC, HER2 IHC 1+, BRCA negative, PD-L1 positive, ECOG 2, "
        "stage IV, prior trastuzumab and paclitaxel, CKD stage II, Hb 10.8."
    )
    r = deidentify(chart)
    for token in ["HER2", "BRCA", "PD-L1", "ECOG 2", "stage IV",
                  "trastuzumab", "paclitaxel", "TNBC", "CKD", "Hb 10.8"]:
        assert token in r.text, f"clinical token destroyed by de-id: {token!r}"


def test_nct_ids_are_preserved():
    # NCT IDs are PUBLIC trial identifiers, not PHI — they must NOT be redacted.
    r = deidentify("Discussed trial NCT04374256 with patient.")
    assert "NCT04374256" in r.text


# --------------------------- end-to-end on real synthetic PHI charts ---------------------------

@pytest.mark.parametrize("fixture", ["phi_chart_her2.txt", "phi_chart_tnbc.txt"])
def test_real_charts_are_scrubbed(fixture):
    path = FIXTURES / fixture
    if not path.exists():
        pytest.skip(f"PHI fixture {fixture} not present (gitignored)")
    raw = path.read_text(encoding="utf-8", errors="ignore")
    r = deidentify(raw)

    # Known identifiers from the two synthetic charts must be gone.
    forbidden = [
        "Maria", "Thompson", "Jane Doe", "0048239", "P-1001",
        "09/25/1961", "05/11/1963", "Patel",
    ]
    leaks = [tok for tok in forbidden if tok in r.text]
    assert not leaks, f"PHI leaked through de-id: {leaks}"

    # And the chart must still be clinically usable.
    assert any(t in r.text for t in ["HER2", "breast", "ECOG", "metastatic"]), \
        "de-id destroyed all clinical signal"
    assert r.total_redactions > 0
