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
    TAG_AGE,
    TAG_DATE,
    TAG_EMAIL,
    TAG_FACILITY,
    TAG_ID,
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


def test_removes_bare_city_name_without_state():
    """Was an xfail ("needs the Presidio NER layer"). The rule layer now carries a
    ~300-city US gazetteer plus locational phrasing, so this is a real assertion."""
    r = deidentify("Patient relocated to Detroit last year.")
    assert "Detroit" not in r.text
    assert TAG_ADDRESS in r.text
    # ...and the same holds for the plain gazetteer hit and the travel phrasing.
    assert "Cleveland" not in deidentify("Patient lives in Cleveland.").text
    assert "Toledo" not in deidentify("Travels from Toledo for treatment.").text


def test_removes_facility_names():
    r = deidentify("Treated at Mercy General Hospital, then Karmanos Cancer Center.")
    assert "Mercy General Hospital" not in r.text
    assert "Karmanos" not in r.text
    assert TAG_FACILITY in r.text


def test_removes_patient_record_id():
    r = deidentify("Patient ID: P-1001 active.")
    assert "P-1001" not in r.text


def test_removes_copied_local_record_ids():
    r = deidentify("SYNTH-LUNG-003 in header; repeated SYNTH-LUNG-003 in footer.")
    assert "SYNTH-LUNG-003" not in r.text
    assert r.redaction_counts[TAG_ID] == 2


def test_removes_insurance_and_device_identifiers():
    r = deidentify("Policy BCBS-772819, device serial DX-99182 implanted.")
    assert "BCBS-772819" not in r.text
    assert "DX-99182" not in r.text


def test_removes_relative_and_signature_names():
    r = deidentify("Mother: Jane Doe. Electronically signed by Dr. Sarah Chen, MD")
    assert "Jane Doe" not in r.text
    assert "Sarah Chen" not in r.text


def test_generalizes_age_over_89_but_keeps_age_in_years():
    over = deidentify("The patient is a 92 year old female.")
    assert "92" not in over.text
    keep = deidentify("The patient is a 64 year old female.")
    assert "64" in keep.text  # ages <= 89 retained for trial age-eligibility matching


def test_generalizes_bare_and_labeled_ages_over_89():
    """The age rule used to require a year(s)-old / yo / yrs suffix, so a labeled or
    bare age over 89 leaked straight through."""
    assert "93" not in deidentify("Age: 93").text
    assert "95" not in deidentify("The patient is 95.").text
    assert TAG_AGE in deidentify("Age: 93").text
    # <= 89 stays put in every surface form
    assert "89" in deidentify("Age: 89").text
    assert "71" in deidentify("The patient is 71.").text


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


@pytest.mark.parametrize("fixture,forbidden,required", [
    (
        "synthetic_chart_her2_positive_breast.txt",
        ["Dana Fictional", "SYNTH-0000-HER2", "01/14/1964", "(555) 0100",
         "Example Cancer Center", "Springfield", "Placeholder Ave", "Dr. Sample"],
        ["HER2 IHC 3+", "ISH amplified", "ECOG 1", "Stage IV", "trastuzumab",
         "paclitaxel", "pertuzumab", "62 year-old", "Hemoglobin 10.9 g/dL",
         "Illinois", "Indiana"],
    ),
    (
        "synthetic_chart_tnbc_her2_low.txt",
        ["Robin Placeholder", "SYNTH-0000-TNBC", "(555) 0142", "05/12/26",
         "05/22/26", "200 Example St"],
        ["HER2 IHC 1+", "PD-L1 CPS 15", "ECOG 2", "capecitabine", "atezolizumab",
         "nab-paclitaxel", "MSI stable", "Hgb 10.8", "Ohio", "Indiana"],
    ),
])
def test_committed_synthetic_charts_lose_identity_but_keep_medicine(
    fixture, forbidden, required
):
    """End-to-end on the charts that ship with the repo: every identifier class in
    them must go, and every clinical fact the matcher depends on must stay."""
    path = FIXTURES / fixture
    if not path.exists():
        pytest.skip(f"fixture {fixture} not present")
    r = deidentify(path.read_text(encoding="utf-8", errors="ignore"))

    leaks = [tok for tok in forbidden if tok in r.text]
    assert not leaks, f"PHI leaked through de-id: {leaks}"

    destroyed = [tok for tok in required if tok not in r.text]
    assert not destroyed, f"de-id over-redacted clinical content: {destroyed}"
