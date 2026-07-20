"""Adversarial de-identification suite.

Probes EVERY HIPAA identifier class the requirements doc names — names, relatives,
facilities, cities, ZIP, dates (incl. month/year), ages > 89, MRN/HRN, SSN, phone,
email, URL, signatures, copied/repeated record IDs, insurance and device IDs — plus
the two failure directions that matter equally:

  * real identifiers MUST NOT leak, and
  * clinical signal MUST survive. Over-redaction that destroys medicine (a biomarker,
    a drug, a stage, a lab value, an imaging modality) is a bug of the same severity
    as a leak, and gets the same number of tests here.

Honest scope: rule-based de-id is redaction *assistance*, not certified Safe Harbor
de-identification. The residual gaps are asserted explicitly at the bottom of this
file (`TestDocumentedLimitations`) so they stay visible and cannot silently regress
into a false sense of safety. See docs/PRIVACY_DATA_FLOW.md.
"""
from __future__ import annotations

import pytest

from app.intake.deident import (
    TAG_ADDRESS,
    TAG_AGE,
    TAG_FACILITY,
    TAG_ID,
    TAG_MRN,
    TAG_NAME,
    TAG_PHONE,
    Deidentifier,
    deidentify,
)


def scrub(raw: str) -> str:
    return deidentify(raw).text


# =============================================================================
# 1. Identifiers MUST be removed — one parametrised case per identifier class
# =============================================================================

@pytest.mark.parametrize("raw,leak", [
    # --- URLs -----------------------------------------------------------------
    ("Portal https://mychart.example.org/p/9", "mychart.example.org"),
    ("See www.facility-clinic.org for info", "facility-clinic.org"),
    # --- email ----------------------------------------------------------------
    ("Email jane.doe@example.com noted.", "jane.doe@example.com"),
    ("contact: r.ellis+chart@hospital.example.net", "r.ellis+chart@hospital.example.net"),
    # --- dates, including month/year ------------------------------------------
    ("Biopsy on May 12, 2026.", "2026"),
    ("Consult 12 August 2026 completed.", "August 2026"),
    ("Started chemo March 2014.", "March 2014"),
    ("Recurrence documented Aug 2019.", "Aug 2019"),
    ("Imaging dated 2026-06-20 reviewed.", "2026-06-20"),
    ("DOB 09/25/1961 on chart.", "09/25/1961"),
    ("Date of Birth: 05/11/1963", "05/11/1963"),
    # --- ZIP ------------------------------------------------------------------
    ("Home ZIP 48201-1234 on file.", "48201-1234"),
    ("Home ZIP 48201-1234 on file.", "1234"),          # no dangling ZIP+4 tail
    ("Residence: Detroit, MI 48226.", "48226"),
    ("Postal code: 90210", "90210"),
    # --- street address -------------------------------------------------------
    ("Lives at 123 North Main Street.", "123 North Main Street"),
    ("Mailing 4400 Woodward Ave, Apt 3B", "4400 Woodward Ave"),
    # --- MRN / HRN ------------------------------------------------------------
    ("MRN 0048239 active.", "0048239"),
    ("HRN: BRE00000273 filed.", "BRE00000273"),
    ("Medical record number 883-22-9910", "883-22-9910"),
    # --- SSN / phone ----------------------------------------------------------
    ("SSN 123-45-6789 on chart.", "123-45-6789"),
    ("Call (313) 555-0142 today.", "555-0142"),
    ("Phone: (555) 0100", "(555) 0100"),
    # --- names: labeled, titled, mid-sentence, role ---------------------------
    ("Patient Name: Maria E. Thompson.", "Thompson"),
    ("Seen by Dr. Patel and A. Okafor MD.", "Patel"),
    ("Seen by Dr. Patel and A. Okafor MD.", "Okafor"),
    ("Patient Maria Gonzalez presented today.", "Gonzalez"),
    ("pt Avery Samplepatient / F / MI", "Samplepatient"),
    ("Nurse Alvarez charted vitals.", "Alvarez"),
    ("Attending Rodriguez reviewed the scan.", "Rodriguez"),
    ("Oncologist Whitfield agrees with the plan.", "Whitfield"),
    ("Technologist Nakamura performed the scan.", "Nakamura"),
    ("RN Delacroix administered the infusion.", "Delacroix"),
    # --- relatives / contacts -------------------------------------------------
    ("Mother: Jane Doe is the emergency contact.", "Jane Doe"),
    ("Next of kin - Robert Ellis, reachable evenings.", "Robert Ellis"),
    ("Daughter Emily Watson accompanies to visits.", "Emily Watson"),
    ("Emergency contact: Priya Raman (spouse).", "Priya Raman"),
    # --- signatures -----------------------------------------------------------
    ("Electronically signed by Sarah Chen, MD", "Sarah Chen"),
    ("Signed by: Alvarez, RN", "Alvarez"),
    ("/s/ Maria Lopez", "Maria Lopez"),
    ("Dictated by Dr. Nwosu, transcribed by Kim Delgado.", "Delgado"),
    # --- facilities / institutions -------------------------------------------
    ("Treated at Mercy General Hospital.", "Mercy General Hospital"),
    ("Referred to Cleveland Clinic Taussig Cancer Institute.", "Taussig"),
    ("Follow-up at the University of Michigan Health System.", "Michigan Health System"),
    ("Infusions given at Karmanos Cancer Center.", "Karmanos"),
    ("Imaging done at Riverbend Medical Center.", "Riverbend"),
    ("Discharged from Saint Agnes Infirmary.", "Agnes"),
    # --- cities ---------------------------------------------------------------
    ("Patient lives in Cleveland.", "Cleveland"),
    ("Travels from Toledo for treatment.", "Toledo"),
    ("Patient relocated to Detroit last year.", "Detroit"),
    ("Residence: Springfield, Ohio.", "Springfield"),
    ("Patient from Detroit, MI prefers local sites.", "Detroit, MI"),
    ("Resides in Ann Arbor with spouse.", "Ann Arbor"),
    # a municipality that is NOT in the gazetteer, caught by locational phrasing
    ("Travels from Grosse Pointe for care.", "Grosse Pointe"),
    # an ambiguous city name, safe to redact *in locational context*
    ("Commutes from Buffalo for infusions.", "Buffalo"),
    # --- ages > 89 ------------------------------------------------------------
    ("A 92 year old man.", "92"),
    ("Age: 93", "93"),
    ("The patient is 95.", "95"),
    ("95-year-old man with NSCLC.", "95"),
    ("91 yo female presented.", "91"),
    # --- copied / repeated local record IDs -----------------------------------
    ("Patient ID: P-1001 active.", "P-1001"),
    ("Barcode 000058755460 scanned.", "000058755460"),
    ("SYNTH-LUNG-003 in header.", "SYNTH-LUNG-003"),
    ("Accession ABC-DEF-2291 in report footer.", "ABC-DEF-2291"),
    # --- insurance / device ---------------------------------------------------
    ("Policy BCBS-772819 verified.", "BCBS-772819"),
    ("Device serial DX-99182 implanted.", "DX-99182"),
    ("Member ID 88213345 on file.", "88213345"),
    ("Group number GRP-44821 active.", "GRP-44821"),
    ("Subscriber #AET90233 primary.", "AET90233"),
    ("Lot 99120A of the implant.", "99120A"),
])
def test_identifier_is_removed(raw, leak):
    assert leak not in scrub(raw), f"identifier leaked: {leak!r}"


# =============================================================================
# 2. Identifiers get the RIGHT tag (tag precedence / auditability)
# =============================================================================

@pytest.mark.parametrize("raw,tag", [
    ("Medical record number 883-22-9910", TAG_MRN),   # labeled MRN beats SSN shape
    ("MRN: 0048239", TAG_MRN),
    ("Treated at Mercy General Hospital.", TAG_FACILITY),
    ("Patient lives in Cleveland.", TAG_ADDRESS),
    ("Age: 93", TAG_AGE),
    ("Nurse Alvarez charted vitals.", TAG_NAME),
    ("Policy BCBS-772819 verified.", TAG_ID),
    ("Phone: (555) 0100", TAG_PHONE),
])
def test_identifier_gets_correct_tag(raw, tag):
    result = deidentify(raw)
    assert tag in result.text, f"expected {tag} in {result.text!r}"
    assert result.redaction_counts.get(tag, 0) >= 1


def test_labeled_mrn_is_not_mistagged_as_ssn():
    """Regression: an explicitly labeled MRN that happens to be formatted like an
    SSN used to come back tagged [SSN], which misleads anyone auditing the counts."""
    result = deidentify("Medical record number 883-22-9910")
    assert TAG_MRN in result.text
    assert "[SSN]" not in result.text


def test_repeated_identifier_is_redacted_every_time():
    """A record ID copied into a header AND a footer must be gone in both places —
    copy-forward is exactly how identifiers survive a sloppy scrub."""
    result = deidentify("SYNTH-LUNG-003 ... repeated SYNTH-LUNG-003 in footer.")
    assert "SYNTH-LUNG-003" not in result.text
    assert result.redaction_counts[TAG_ID] == 2


def test_age_over_89_generalized_but_working_age_kept():
    assert "92" not in scrub("A 92 year old man.")
    # ages <= 89 are needed for trial age-eligibility and must NOT be touched
    assert "64" in scrub("A 64 year old woman.")


@pytest.mark.parametrize("raw", ["Age: 89", "The patient is 89.", "89 year old male",
                                 "A 64 year old woman.", "Age 71", "72-year-old"])
def test_age_at_or_below_89_is_never_redacted(raw):
    assert TAG_AGE not in scrub(raw), f"HIPAA only generalizes ages > 89: {raw!r}"


def test_redaction_counts_are_auditable():
    result = deidentify("Patient Maria Gonzalez, MRN 0048239, seen at Mercy General "
                        "Hospital, lives in Cleveland.")
    assert result.total_redactions == sum(result.redaction_counts.values())
    assert set(result.redaction_counts) >= {TAG_NAME, TAG_MRN, TAG_FACILITY, TAG_ADDRESS}
    assert "Redacted:" in result.summary()


# =============================================================================
# 3. Clinical signal MUST survive — over-redaction is a bug, not safety
# =============================================================================

def test_clinical_signal_survives_hardening():
    chart = ("HER2 IHC 3+, BRCA negative, PD-L1 CPS 15, ECOG 1, stage IV, "
             "trastuzumab and paclitaxel; may improve on 5 mg dosing.")
    out = scrub(chart)
    for token in ["HER2", "BRCA", "PD-L1", "ECOG 1", "stage IV", "trastuzumab", "5 mg"]:
        assert token in out, f"clinical token destroyed: {token!r}"


@pytest.mark.parametrize("phrase", [
    # biomarkers and hyphenated clinical tokens the record-ID rules must not eat
    "PD-L1 CPS 15", "T-DM1", "5-FU", "CTLA-4", "COVID-19", "HER2-low", "nab-paclitaxel",
    "R-CHOP", "ddAC", "AC->taxol", "KRAS G12C", "EGFR Exon 19 deletion", "ALK",
    # staging / performance
    "stage IV", "Stage IVB", "ECOG 1", "KPS 80", "grade 3",
    # drugs
    "osimertinib", "carboplatin plus pemetrexed", "trastuzumab emtansine",
    "atezolizumab", "capecitabine",
    # labs and vitals
    "Hemoglobin 10.9 g/dL", "ANC 4.1", "Plt 268", "Cr 1.1", "AST 42", "Tbili 0.7",
    "Platelets 240 10*3/uL", "Total bilirubin 0.8 mg/dL",
    # public trial IDs are not PHI
    "NCT04374256",
])
def test_clinical_token_is_never_redacted(phrase):
    assert phrase in scrub(f"Assessment note: {phrase} documented today."), \
        f"clinical token destroyed by de-id: {phrase!r}"


@pytest.mark.parametrize("sentence", [
    # city names that double as ordinary English / anatomy / devices
    "The patient is mobile and independent in ADLs.",
    "Buffalo hump noted on exam.",
    "Corona radiata infarct seen on MRI.",
    "Jackson-Pratt drain removed on POD 3.",
    "Allen test negative bilaterally.",
    "Reading of the ECG is normal.",
    "Sister Mary Joseph nodule at the umbilicus.",
    # structural / prose text that name rules must not swallow
    "Serum Tumor Markers within reference range.",
    "Chronic Liver Disease and Diabetes Mellitus noted.",
    "Do not infer eligibility, recommend treatment, or predict outcome.",
    "Patient reports fatigue and denies tobacco use.",
    # imaging modality after a number must not read as a street suffix
    "The current specimen is the 2026 lung core. CT shows pulmonary nodules.",
])
def test_clinical_prose_is_not_over_redacted(sentence):
    assert scrub(sentence) == sentence, "de-id altered non-identifying clinical text"


def test_scrubbing_is_idempotent():
    """`/api/match` re-scrubs already-scrubbed text as defense-in-depth, so a second
    pass must be a no-op. If it is not, the rules are chewing on their own tags."""
    raw = (
        "Patient Maria Gonzalez | MRN 0048239 | DOB 09/25/1961 | Phone (313) 555-0142\n"
        "Seen at Mercy General Hospital, lives in Cleveland. Age: 93.\n"
        "HER2 IHC 3+, PD-L1 CPS 15, ECOG 1, stage IV, trastuzumab."
    )
    once = scrub(raw)
    assert scrub(once) == once, "re-scrubbing a scrubbed chart changed it"


def test_state_level_geography_is_preserved():
    """Safe Harbor permits state, and the matcher needs it for site preferences."""
    out = scrub("Prefers Michigan, Ohio, or Illinois; approximately 350-mile radius.")
    for state in ["Michigan", "Ohio", "Illinois"]:
        assert state in out, f"state-level preference destroyed: {state}"


def test_whole_chart_keeps_clinical_meaning_while_losing_identity():
    chart = (
        "ONCOLOGY CONSULT NOTE\n"
        "Patient Maria Gonzalez | MRN 0048239 | DOB 09/25/1961 | Phone (313) 555-0142\n"
        "Seen at Mercy General Hospital; lives in Cleveland. Mother: Jane Doe.\n"
        "Electronically signed by Dr. Patel.\n"
        "64 year old female, metastatic TNBC, HER2 IHC 1+, BRCA negative, "
        "PD-L1 CPS 15, ECOG 2, stage IV, prior trastuzumab and paclitaxel, Hb 10.8.\n"
    )
    out = scrub(chart)
    for leak in ["Maria", "Gonzalez", "0048239", "09/25/1961", "555-0142",
                 "Mercy General Hospital", "Cleveland", "Jane Doe", "Patel"]:
        assert leak not in out, f"PHI leaked: {leak!r}"
    for keep in ["64 year old", "TNBC", "HER2", "BRCA", "PD-L1", "ECOG 2",
                 "stage IV", "trastuzumab", "paclitaxel", "Hb 10.8"]:
        assert keep in out, f"clinical signal destroyed: {keep!r}"


# =============================================================================
# 4. Optional Presidio NER layer — exercised WITHOUT Presidio installed
# =============================================================================

class _StubSpan:
    """Mimics presidio_analyzer.RecognizerResult (start/end/entity_type/score)."""

    def __init__(self, start, end, entity_type, score=0.9):
        self.start, self.end = start, end
        self.entity_type, self.score = entity_type, score


class _StubAnalyzer:
    def __init__(self, spans):
        self._spans = spans
        self.requested_entities = None

    def analyze(self, text, entities, language):
        self.requested_entities = list(entities)
        return self._spans


def test_presidio_layer_requests_person_location_and_organization():
    """Regression for the audit's finding: the layer the docs advertise as the
    mitigation for free-text cities used to request PERSON only, so it could not
    possibly have caught a location or an institution."""
    analyzer = _StubAnalyzer([])
    Deidentifier(presidio_analyzer=analyzer).deidentify("some text")
    assert set(analyzer.requested_entities) == {"PERSON", "LOCATION", "ORGANIZATION"}


def test_presidio_entities_map_to_the_right_tags():
    text = "Zbigniew Wojciechowski moved to Ypsilanti and is treated at Bayfront."
    spans = [
        _StubSpan(text.index("Zbigniew"), text.index("Zbigniew") + 22, "PERSON"),
        _StubSpan(text.index("Ypsilanti"), text.index("Ypsilanti") + 9, "LOCATION"),
        _StubSpan(text.index("Bayfront"), text.index("Bayfront") + 8, "ORGANIZATION"),
    ]
    result = Deidentifier(presidio_analyzer=_StubAnalyzer(spans)).deidentify(text)
    assert "Zbigniew" not in result.text and "Ypsilanti" not in result.text
    assert "Bayfront" not in result.text
    assert TAG_NAME in result.text
    assert TAG_ADDRESS in result.text
    assert TAG_FACILITY in result.text


def test_presidio_low_confidence_spans_are_ignored():
    text = "Metastatic disease progression."
    spans = [_StubSpan(0, 10, "PERSON", score=0.2)]
    result = Deidentifier(presidio_analyzer=_StubAnalyzer(spans)).deidentify(text)
    assert result.text == text


def test_presidio_never_rewrites_an_already_emitted_tag():
    raw = "Patient Maria Gonzalez presented."
    rule_only = scrub(raw)                       # "Patient [NAME] presented."
    start = rule_only.index(TAG_NAME)
    spans = [_StubSpan(start, start + len(TAG_NAME), "PERSON")]
    result = Deidentifier(presidio_analyzer=_StubAnalyzer(spans)).deidentify(raw)
    assert result.text == rule_only, "NER re-redacted a tag the rules already emitted"
    assert result.text.count(TAG_NAME) == 1


def test_presidio_failure_degrades_to_rule_layer():
    class _Exploding:
        def analyze(self, text, entities, language):
            raise RuntimeError("spaCy model missing")

    result = Deidentifier(presidio_analyzer=_Exploding()).deidentify("MRN 0048239 active.")
    assert "0048239" not in result.text, "rule layer must survive an NER failure"


def test_rule_layer_is_unchanged_when_presidio_is_absent():
    """Presidio is NOT installed in this venv — the always-on rule layer must give
    identical output either way."""
    raw = "Patient Maria Gonzalez, MRN 0048239, lives in Cleveland."
    assert Deidentifier(use_presidio=False).deidentify(raw).text == deidentify(raw).text


# =============================================================================
# 5. Documented limitations — asserted so they cannot silently change
# =============================================================================

class TestDocumentedLimitations:
    """These are the gaps we chose NOT to close, each for a stated reason. They are
    written as assertions of CURRENT behaviour so that the docs and the code cannot
    drift apart: if one of these starts passing, update PRIVACY_DATA_FLOW.md."""

    def test_bare_five_digit_number_is_not_treated_as_a_zip(self):
        # Deliberate: bare 5-digit numbers collide with lab values, doses and
        # accession fragments. ZIPs are caught with a label, a ZIP+4, or after a
        # state / redacted place. See PRIVACY_DATA_FLOW.md "Known limitations".
        assert "48226" in scrub("The code is 48226")
        # ...but the same number IS caught once it carries geographic context:
        assert "48226" not in scrub("Detroit, MI 48226")
        assert "48226" not in scrub("ZIP 48226")

    def test_off_gazetteer_city_without_locational_phrasing_can_pass_through(self):
        # A city that is neither in the ~300-city gazetteer, nor followed by a
        # state, nor in a locational phrase is not detectable by rules alone.
        # Presidio's LOCATION entity is the mitigation.
        assert "Ypsilanti" in scrub("Ypsilanti was mentioned in the note.")

    def test_unlabeled_uncommon_name_can_pass_through_in_rules_only_mode(self):
        # Name detection is positive-signal (given-name list / middle initial /
        # role / label). An unlabeled, uncommon full name with no signal survives.
        assert "Zbigniew" in scrub("Zbigniew Wojciechowski was in the waiting room.")
