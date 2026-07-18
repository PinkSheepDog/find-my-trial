"""De-identification — the HIPAA safety boundary.

This module strips the 18 HIPAA Safe Harbor identifiers (45 CFR 164.514(b)(2))
from chart text BEFORE it can be sent to any external service (the LLM).

Design:
  * A deterministic RULE layer is ALWAYS on. It needs no models, no network,
    runs offline, and is fully testable. It covers the structured/format-bearing
    identifiers (MRN/HRN, SSN, dates, phone, email, addresses, IDs) plus name
    patterns with a clear signal ("Dr. X", "Name: X", "First M. Last", or a
    "Firstname Lastname" whose first token is a known given name).
  * An optional NER layer (Presidio/spaCy) catches free-text person names the
    rules miss. It is additive — never required — and the system degrades
    cleanly to rule-only if Presidio is not installed.

CRITICAL: clinically meaningful tokens must survive de-identification. Biomarker
status ("HER2", "BRCA negative"), drug names, ECOG, stage, ages-in-years, lab
values, and section/label text are NOT identifiers and must NOT be redacted. We
redact identity, not medicine. Over-redaction that destroys clinical signal is
treated as a bug, equal in severity to a leak.

Name detection is POSITIVE-SIGNAL, not denylist-based: a bare "Word Word" phrase
is only redacted as a name when it carries a name signal (middle initial, or a
first token that is a known given name). This is what keeps documents dense with
Title Case medical terms ("Serum Tumor Markers", "Chronic Liver Disease") intact.
The trade-off: an unlabeled name with an uncommon first name and no middle initial
can slip through in rules-only mode — enable Presidio (FMT_USE_PRESIDIO=true) for
free-text-heavy inputs, and note that /api/match re-scrubs as defense-in-depth.

Ages: HIPAA Safe Harbor requires ages > 89 be generalized. Ages <= 89 in years
are retained (they are needed for trial age-eligibility matching).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Redaction tags. Distinct tags keep the de-identified text readable and let the
# extractor still understand structure (a [DATE] is still "a date happened here").
# ---------------------------------------------------------------------------
TAG_NAME = "[NAME]"
TAG_DATE = "[DATE]"
TAG_MRN = "[MRN]"
TAG_SSN = "[SSN]"
TAG_PHONE = "[PHONE]"
TAG_EMAIL = "[EMAIL]"
TAG_ADDRESS = "[ADDRESS]"
TAG_ID = "[ID]"
TAG_AGE = "[AGE>89]"
TAG_URL = "[URL]"


# Clinical/structural tokens that can look like names (capitalized) but never are.
# This is a SECONDARY guard; the primary name gate is the given-name signal below.
_CLINICAL_SAFEWORDS = {
    "ecog", "her2", "egfr", "alk", "ros1", "braf", "kras", "brca", "brca1", "brca2",
    "pd", "pdl1", "msi", "hrd", "tnbc", "her2-low", "ihc", "fish", "ca", "cea",
    "ct", "mri", "pet", "wbc", "hb", "plt", "alt", "ast", "bun", "ldh", "egf",
    "stage", "grade", "cycle", "mg", "ml", "kg", "cm", "bid", "tid", "qid", "prn",
    "ned", "sob", "hfs", "qol", "dm", "dm2", "htn", "ckd", "oa", "kps", "bmi",
    "icd", "nct", "phase", "arm", "dose", "iv", "po", "left", "right", "lung",
    "liver", "bone", "brain", "breast", "node", "lobe",
}

# Common English / report / medical words that are frequently Title-Cased in
# documents (section headers, lab names, comorbidities). If any token of a
# candidate "name" is in here, it is not a person name. Belt-and-suspenders on
# top of the given-name gate.
_COMMON_NON_NAME_WORDS = {
    "patient", "information", "identification", "data", "general", "biochemistry",
    "blood", "drawn", "realization", "date", "name", "personal", "comorbidities",
    "hemolized", "hemolyzed", "sample", "serum", "urine", "tumor", "tumour",
    "markers", "marker", "lifestyle", "atherosclerosis", "chronic", "liver",
    "disease", "diabetes", "mellitus", "jaundice", "renal", "failure", "smoking",
    "negative", "positive", "outcome", "results", "result", "some", "reference",
    "range", "low", "moderate", "high", "comments", "comment", "absence", "false",
    "healthy", "patients", "increasing", "whole", "levels", "level", "suggest",
    "malignancy", "conclusions", "conclusion", "cancer", "diagnosis", "code",
    "malignant", "neoplasm", "report", "generated", "entered", "disclaimer",
    "multiple", "biomarkers", "biomarker", "activity", "algorithm", "developed",
    "exclusive", "healthcare", "professionals", "solely", "clinical", "decision",
    "support", "system", "unique", "element", "sensitivity", "specificity",
    "please", "note", "negativity", "possibility", "epithelial", "royal",
    "hospital", "technical", "responsible", "chief", "scientific", "officer",
    "email", "website", "powered", "creatinine", "bilirubin", "total", "age",
    "years", "final", "review", "summary", "history", "physical", "exam",
    "assessment", "plan", "impression", "medications", "allergies", "vitals",
    "laboratory", "labs", "pathology", "radiology", "oncology", "hematology",
    "department", "medical", "center", "clinic", "university", "national",
    "institute", "records", "record", "number",
}

_NON_NAME_WORDS = _CLINICAL_SAFEWORDS | _COMMON_NON_NAME_WORDS

# Common given names (English + widely-seen international). Used as a POSITIVE
# signal: an unlabeled "Firstname Lastname" is only redacted when its first token
# is a plausible given name. This is bounded and stable, unlike enumerating all
# medical vocabulary. Not exhaustive by design — Presidio covers the long tail.
_GIVEN_NAMES = {
    # Female
    "mary", "patricia", "jennifer", "linda", "elizabeth", "barbara", "susan",
    "jessica", "sarah", "karen", "nancy", "lisa", "margaret", "sandra", "ashley",
    "kimberly", "emily", "donna", "michelle", "carol", "amanda", "dorothy",
    "melissa", "deborah", "stephanie", "rebecca", "laura", "sharon", "cynthia",
    "kathleen", "helen", "amy", "angela", "anna", "ruth", "brenda", "pamela",
    "nicole", "katherine", "virginia", "catherine", "christine", "samantha",
    "debra", "janet", "carolyn", "rachel", "heather", "diane", "julie", "emma",
    "olivia", "sophia", "isabella", "ava", "mia", "charlotte", "amelia", "harper",
    "evelyn", "abigail", "ella", "sofia", "grace", "chloe", "victoria", "lily",
    "maria", "lorena", "lucia", "elena", "olga", "natasha", "fatima", "aisha",
    "priya", "ananya", "neha", "pooja", "claire", "marie", "greta", "ingrid",
    "francesca", "giulia", "jane", "joan", "judith", "megan", "hannah", "zoe",
    # Male
    "james", "john", "robert", "michael", "william", "david", "richard", "joseph",
    "thomas", "charles", "christopher", "daniel", "matthew", "anthony", "mark",
    "donald", "steven", "paul", "andrew", "joshua", "kenneth", "kevin", "brian",
    "george", "timothy", "ronald", "edward", "jason", "jeffrey", "ryan", "jacob",
    "gary", "nicholas", "eric", "jonathan", "stephen", "larry", "justin", "scott",
    "brandon", "benjamin", "samuel", "gregory", "alexander", "patrick", "frank",
    "raymond", "jack", "dennis", "jerry", "tyler", "aaron", "jose", "henry",
    "adam", "douglas", "nathan", "peter", "zachary", "kyle", "walter", "noah",
    "liam", "ethan", "mason", "logan", "lucas", "oliver", "elijah", "carlos",
    "juan", "luis", "miguel", "jorge", "pedro", "marco", "giovanni", "giuseppe",
    "mohammed", "mohamed", "ahmed", "ali", "hassan", "omar", "yusuf", "ibrahim",
    "raj", "amit", "sanjay", "anil", "sunil", "ravi", "deepak", "arjun", "rohan",
    "wei", "chen", "hiroshi", "kenji", "ivan", "dimitri", "pierre", "jean", "hans",
    "klaus", "lars", "erik", "sven", "antoine",
}

# Month names so we can catch "March 2014", "Aug 2019", etc.
_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)

# US state names / common abbreviations used to detect address lines like "Detroit, MI".
_US_STATES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|"
    "georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|"
    "massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|"
    "new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|"
    "oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|"
    "texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming"
)
_STATE_ABBR = (
    r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|"
    r"NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY"
)


@dataclass
class DeidResult:
    """Result of de-identification, including an audit trail of what was removed.
    The counts (not the values) are safe to log for monitoring."""

    text: str
    redaction_counts: dict[str, int] = field(default_factory=dict)

    @property
    def total_redactions(self) -> int:
        return sum(self.redaction_counts.values())

    def summary(self) -> str:
        if not self.total_redactions:
            return "No identifiers detected."
        parts = [f"{n}x {tag}" for tag, n in sorted(self.redaction_counts.items())]
        return "Redacted: " + ", ".join(parts)


class Deidentifier:
    """Rule-based de-identifier (always on) with optional Presidio NER augmentation."""

    def __init__(self, use_presidio: bool = False) -> None:
        self._presidio = _try_load_presidio() if use_presidio else None

    def deidentify(self, text: str) -> DeidResult:
        if not text:
            return DeidResult(text="", redaction_counts={})

        counts: dict[str, int] = {}

        def sub(pattern: re.Pattern[str], tag: str, s: str) -> str:
            def _repl(_m: re.Match[str]) -> str:
                counts[tag] = counts.get(tag, 0) + 1
                return tag
            return pattern.sub(_repl, s)

        out = text

        # --- Order matters: most specific / format-bearing first ---
        out = sub(_RE_URL, TAG_URL, out)             # http(s):// and www. links
        out = sub(_RE_EMAIL, TAG_EMAIL, out)
        out = sub(_RE_SSN, TAG_SSN, out)
        out = sub(_RE_PHONE, TAG_PHONE, out)
        out = sub(_RE_MRN, TAG_MRN, out)             # labeled MRN/HRN (numeric or alphanumeric)
        out = sub(_RE_NCT_OTHER_ID, TAG_ID, out)     # patient-side record IDs (P-1001 etc.)
        out = self._redact_ages_over_89(out, counts)
        out = sub(_RE_DOB_LABELED, TAG_DATE, out)
        out = sub(_RE_DATE_NUMERIC, TAG_DATE, out)
        out = sub(_RE_DATE_MONTH_YEAR, TAG_DATE, out)
        out = sub(_RE_DATE_TEXT, TAG_DATE, out)      # "May 12, 2026", "12 May 2026"
        out = sub(_RE_LONG_NUMERIC_ID, TAG_ID, out)  # bare long digit runs (barcodes/record #s)
        out = sub(_RE_STREET, TAG_ADDRESS, out)      # "123 Main St"
        out = sub(_RE_ZIP, TAG_ADDRESS, out)         # ZIP / ZIP+4
        out = sub(_RE_ADDRESS_CITY_STATE, TAG_ADDRESS, out)
        out = sub(_RE_LABELED_NAME, TAG_NAME, out)
        out = sub(_RE_TITLED_NAME, TAG_NAME, out)
        out = self._redact_freetext_names(out, counts)

        # --- Optional NER pass for residual free-text person names ---
        if self._presidio is not None:
            out, ner_count = self._presidio_names(out)
            if ner_count:
                counts[TAG_NAME] = counts.get(TAG_NAME, 0) + ner_count

        return DeidResult(text=out, redaction_counts=counts)

    # -- ages > 89 -> generalized; ages <= 89 retained for matching --
    @staticmethod
    def _redact_ages_over_89(text: str, counts: dict[str, int]) -> str:
        def _repl(m: re.Match[str]) -> str:
            age = int(m.group("age"))
            if age > 89:
                counts[TAG_AGE] = counts.get(TAG_AGE, 0) + 1
                return TAG_AGE + m.group("suffix")
            return m.group(0)
        return _RE_AGE.sub(_repl, text)

    def _redact_freetext_names(self, text: str, counts: dict[str, int]) -> str:
        """Catch bare person names ("Maria E. Thompson", "Maria Khan") that carry no
        label or title. POSITIVE-SIGNAL only: a candidate is redacted as a name only
        when it has a middle initial OR its first token is a known given name, and
        never when any token is a common clinical/structural word. This preserves
        Title-Case medical text ("Serum Tumor Markers", "Chronic Liver Disease")."""

        def _repl(m: re.Match[str]) -> str:
            phrase = m.group(0)
            # A field label ("Total Bilirubin:") is structural, not a name.
            if m.string[m.end():m.end() + 1] == ":":
                return phrase
            tokens = [t for t in re.split(r"[ \t.]+", phrase) if t]
            low = [t.lower() for t in tokens]
            # Any common/clinical word present -> not a person name.
            if any(t in _NON_NAME_WORDS for t in low):
                return phrase
            has_middle_initial = m.group("mi") is not None
            first_is_given = low[0] in _GIVEN_NAMES
            if not (has_middle_initial or first_is_given):
                return phrase  # no name signal -> leave it (avoid clinical destruction)
            counts[TAG_NAME] = counts.get(TAG_NAME, 0) + 1
            return TAG_NAME

        return _RE_FREETEXT_NAME.sub(_repl, text)

    def _presidio_names(self, text: str) -> tuple[str, int]:
        analyzer, anonymizer = self._presidio
        try:
            results = analyzer.analyze(text=text, entities=["PERSON"], language="en")
            results = [r for r in results if r.score >= 0.6]
            if not results:
                return text, len(results)
            from presidio_anonymizer.entities import OperatorConfig
            anonymized = anonymizer.anonymize(
                text=text,
                analyzer_results=results,
                operators={"PERSON": OperatorConfig("replace", {"new_value": TAG_NAME})},
            )
            return anonymized.text, len(results)
        except Exception:
            # NER is best-effort; never let it break the always-on rule layer.
            return text, 0


# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------
_RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

_RE_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

_RE_PHONE = re.compile(
    r"(?:\+?1[\s.\-]?)?(?:\(\d{3}\)\s*|\d{3}[\s.\-])\d{3}[\s.\-]\d{4}\b"
)

# MRN / HRN / medical-record numbers. Labeled, value may be numeric OR alphanumeric
# ("BRE00000273"), and the value may sit on the next line (tabular reports). Crosses
# at most one line break to reach the value.
_RE_MRN = re.compile(
    r"\b(?:MRN|HRN|Medical\s+Record(?:\s+Number)?|Hospital\s+Record(?:\s+Number)?|"
    r"Record\s*(?:Number|No\.?|#))"
    r"[ \t]*[:#]?[ \t]*\n?[ \t]*"
    r"(?:[A-Za-z]{1,5})?\d[A-Za-z0-9\-]{3,}\b",
    re.IGNORECASE,
)

# Patient-side record identifiers like "P-1001", "Patient ID: P-1001".
# Deliberately excludes NCT IDs (those are public trial IDs, not PHI).
_RE_NCT_OTHER_ID = re.compile(
    r"\b(?:Patient\s*ID|Pt\s*ID|Account|Acct)\s*[:#]?\s*[A-Za-z]?-?\d{3,}\b"
    r"|\bP-\d{3,}\b",
    re.IGNORECASE,
)

# Age: capture number + the unit suffix so we can re-attach for <=89.
_RE_AGE = re.compile(
    r"\b(?P<age>\d{1,3})(?P<suffix>\s*(?:year[s]?[- ]old|y/?o|yo|yrs?[- ]old|yrs))\b",
    re.IGNORECASE,
)

# DOB lines: "DOB 09/25/1961", "Date of Birth: 05/11/1963"
_RE_DOB_LABELED = re.compile(
    r"\b(?:DOB|Date\s+of\s+Birth|D\.O\.B\.?)\s*[:#]?\s*\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b",
    re.IGNORECASE,
)

# Numeric dates: 09/25/1961, 3-14-2014, 2014/03/01, 25-01-2020
_RE_DATE_NUMERIC = re.compile(
    r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b|\b\d{4}[/\-]\d{1,2}[/\-]\d{1,2}\b"
)

# Month-year: "March 2014", "Aug 2019" (dates more specific than a year are PHI).
_RE_DATE_MONTH_YEAR = re.compile(
    rf"\b(?:{_MONTHS})\.?\s+\d{{4}}\b", re.IGNORECASE
)

# Text dates WITH a year: "May 12, 2026", "12 May 2026", "May 12th 2026". A year is
# required so prose like "may 5 mg" is not mistaken for a date.
_RE_DATE_TEXT = re.compile(
    rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\.?,?\s+\d{{4}}\b",
    re.IGNORECASE,
)

# URLs (http/https and bare www.). Not the same as the OpenRouter egress point —
# these are identifiers copied into charts (portals, facility sites).
_RE_URL = re.compile(r"\bhttps?://[^\s<>\")]+|\bwww\.[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?:/[^\s<>\")]*)?")

# Street address: number + street name + a street-type suffix.
_RE_STREET = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z][A-Za-z0-9.'\-]*\s+){0,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|"
    r"Way|Place|Pl|Terrace|Ter|Circle|Cir|Highway|Hwy|Parkway|Pkwy|Suite|Ste|Apt)\b\.?",
    re.IGNORECASE,
)

# ZIP: unambiguous ZIP+4 anywhere; a bare 5-digit only right after a US state.
_RE_ZIP = re.compile(
    rf"\b\d{{5}}-\d{{4}}\b"
    rf"|(?<=\b(?:{_STATE_ABBR})\s)\d{{5}}\b"
    rf"|(?i:zip|postal)\s*(?:code)?\s*[:#]?\s*\d{{5}}\b"
)

# Bare long digit runs (>=9 digits): barcodes, document control numbers, record IDs.
# Specific formats (phone, SSN, MRN, dates) are handled above and run first.
_RE_LONG_NUMERIC_ID = re.compile(r"\b\d{9,}\b")

# City, State[, country]: "Detroit, MI", "Detroit, Michigan"
_RE_ADDRESS_CITY_STATE = re.compile(
    rf"\b[A-Z][a-zA-Z]+,\s*(?:{_US_STATES}|{_STATE_ABBR})\b",
    re.IGNORECASE,
)

# Labeled names: "Patient Name: Jane Doe", "Name: Maria E. Thompson". The label and
# value may be on separate lines (crosses at most one line break); the name tokens
# themselves stay on a single line so we don't swallow the next line's content.
_RE_LABELED_NAME = re.compile(
    r"\b(?:Patient\s+Name|Name|Pt\s+Name)[ \t]*[:#][ \t]*\n?[ \t]*"
    r"[A-Z][a-zA-Z'\-]+(?:[ \t]+[A-Z]\.?)?(?:[ \t]+[A-Z][a-zA-Z'\-]+){1,2}",
)

# Titled names: "Dr. Patel", "Dr. Lin", "A. Patel MD"
_RE_TITLED_NAME = re.compile(
    r"\b(?:Dr|Doctor|Mr|Mrs|Ms|Prof)\.?[ \t]+[A-Z][a-zA-Z'\-]+(?:[ \t]+[A-Z][a-zA-Z'\-]+)?"
    r"|\b[A-Z]\.[ \t]*[A-Z][a-zA-Z'\-]+[ \t]+(?:MD|DO|PhD|RN|NP|PA)\b",
)


# Free-text personal names: "First Last" or "First M. Last" (2-3 capitalized tokens,
# optional middle initial), on a SINGLE line. Whether it is actually redacted is
# decided in _redact_freetext_names (given-name / middle-initial signal). The "mi"
# group flags the middle-initial form, a strong name signal.
_RE_FREETEXT_NAME = re.compile(
    r"\b[A-Z][a-z]{1,}[ \t]+(?:(?P<mi>[A-Z])\.[ \t]+)?[A-Z][a-z]{1,}"
    r"(?:[ \t]+[A-Z][a-z]{1,})?\b"
)


def _try_load_presidio():  # pragma: no cover - depends on optional install
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine
        return AnalyzerEngine(), AnonymizerEngine()
    except Exception:
        return None


_default: Deidentifier | None = None


def deidentify(text: str, use_presidio: bool = False) -> DeidResult:
    """Module-level convenience wrapper using a cached default de-identifier."""
    global _default
    if _default is None:
        _default = Deidentifier(use_presidio=use_presidio)
    return _default.deidentify(text)
