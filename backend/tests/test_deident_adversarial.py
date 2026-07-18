"""Adversarial de-identification suite (feedback P0 #3).

Probes the identifier classes the review called out — names, ZIP, dates, MRN/HRN,
email, URL, ages>89 — plus the two failure directions that matter equally:
clinical signal MUST survive, and real identifiers MUST NOT leak.

Honest scope: rule-based de-id is redaction *assistance*. Free-text city names with
no state and unusual unlabeled person names need the optional Presidio NER layer;
one such gap is documented as an xfail so the limitation stays visible, not hidden.
"""
from __future__ import annotations

import pytest

from app.intake.deident import deidentify


@pytest.mark.parametrize("raw,leak", [
    ("Portal https://mychart.example.org/p/9", "mychart.example.org"),
    ("See www.facility-clinic.org for info", "facility-clinic.org"),
    ("Biopsy on May 12, 2026.", "2026"),
    ("Consult 12 August 2026 completed.", "August 2026"),
    ("Home ZIP 48201-1234 on file.", "48201-1234"),
    ("Residence: Detroit, MI 48226.", "48226"),
    ("Lives at 123 North Main Street.", "123 North Main Street"),
    ("MRN 0048239 active.", "0048239"),
    ("HRN: BRE00000273 filed.", "BRE00000273"),
    ("Email jane.doe@example.com noted.", "jane.doe@example.com"),
    ("SSN 123-45-6789 on chart.", "123-45-6789"),
    ("Call (313) 555-0142 today.", "555-0142"),
    ("Patient Name: Maria E. Thompson.", "Thompson"),
    ("Seen by Dr. Patel and A. Okafor MD.", "Patel"),
    ("Barcode 000058755460 scanned.", "000058755460"),
])
def test_identifier_is_removed(raw, leak):
    assert leak not in deidentify(raw).text, f"identifier leaked: {leak!r}"


def test_age_over_89_generalized_but_working_age_kept():
    assert "92" not in deidentify("A 92 year old man.").text
    assert "64" in deidentify("A 64 year old woman.").text  # needed for age-eligibility


def test_clinical_signal_survives_hardening():
    chart = ("HER2 IHC 3+, BRCA negative, PD-L1 CPS 15, ECOG 1, stage IV, "
             "trastuzumab and paclitaxel; may improve on 5 mg dosing.")
    out = deidentify(chart).text
    for token in ["HER2", "BRCA", "PD-L1", "ECOG 1", "stage IV", "trastuzumab", "5 mg"]:
        assert token in out, f"clinical token destroyed: {token!r}"


@pytest.mark.xfail(reason="Bare city names (no state) need the Presidio NER layer; "
                          "documented limitation, not silently 'safe'.", strict=False)
def test_bare_city_name_without_state():
    # A city with no state qualifier is not caught by rules alone.
    assert "Detroit" not in deidentify("Patient relocated to Detroit last year.").text
