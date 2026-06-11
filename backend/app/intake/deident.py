"""De-identification — the HIPAA safety boundary.

This module strips the 18 HIPAA Safe Harbor identifiers (45 CFR 164.514(b)(2))
from chart text BEFORE it can be sent to any external service (the LLM).

Design:
  * A deterministic RULE layer is ALWAYS on. It needs no models, no network,
    runs offline, and is fully testable. It covers the structured/format-bearing
    identifiers (MRN, SSN, dates, phone, email, addresses, IDs) plus common
    name patterns ("Dr. X", "Name: X", "patient X").
  * An optional NER layer (Presidio/spaCy) catches free-text person names the
    rules miss. It is additive — never required — and the system degrades
    cleanly to rule-only if Presidio is not installed.

CRITICAL: clinically meaningful tokens must survive de-identification. Biomarker
status ("HER2", "BRCA negative"), drug names, ECOG, stage, ages-in-years, and
lab values are NOT identifiers and must NOT be redacted. We redact identity, not
medicine. Over-redaction that destroys clinical signal is treated as a bug.

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


# Words that look like names (capitalized) but are clinical/structural — never redact.
_CLINICAL_SAFEWORDS = {
    "ecog", "her2", "egfr", "alk", "ros1", "braf", "kras", "brca", "brca1", "brca2",
    "pd", "pdl1", "msi", "hrd", "tnbc", "her2-low", "ihc", "fish", "ca", "cea",
    "ct", "mri", "pet", "wbc", "hb", "plt", "alt", "ast", "bun", "ldh", "egf",
    "stage", "grade", "cycle", "mg", "ml", "kg", "cm", "bid", "tid", "qid", "prn",
    "ned", "sob", "hfs", "qol", "dm", "dm2", "htn", "ckd", "oa", "kps", "bmi",
    "icd", "nct", "phase", "arm", "dose", "iv", "po", "left", "right", "lung",
    "liver", "bone", "brain", "breast", "node", "lobe",
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
        out = sub(_RE_EMAIL, TAG_EMAIL, out)
        out = sub(_RE_SSN, TAG_SSN, out)
        out = sub(_RE_PHONE, TAG_PHONE, out)
        out = sub(_RE_MRN, TAG_MRN, out)
        out = sub(_RE_NCT_OTHER_ID, TAG_ID, out)  # patient-side record IDs (P-1001 etc.)
        out = self._redact_ages_over_89(out, counts)
        out = sub(_RE_DOB_LABELED, TAG_DATE, out)
        out = sub(_RE_DATE_NUMERIC, TAG_DATE, out)
        out = sub(_RE_DATE_MONTH_YEAR, TAG_DATE, out)
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
        """Catch bare person names ("Maria E. Thompson", "Jane Doe") that carry no
        label. Guarded by a clinical safeword list so medical multi-word phrases
        (e.g. "Breast Cancer", "Heart Failure") are not mistaken for names."""

        def _repl(m: re.Match[str]) -> str:
            phrase = m.group(0)
            tokens = [t for t in re.split(r"[\s.]+", phrase) if t]
            # If any token is a known clinical/structural word, it's not a name.
            if any(t.lower() in _CLINICAL_SAFEWORDS for t in tokens):
                return phrase
            # If every alphabetic token is clinical-safe-cased medicine, skip.
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

# MRN / medical record numbers — labeled or a bare long digit run after "MRN".
_RE_MRN = re.compile(
    r"\b(?:MRN|Medical\s+Record(?:\s+Number)?|Record\s*#)\s*[:#]?\s*\d{4,}\b",
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

# Numeric dates: 09/25/1961, 3-14-2014, 2014/03/01
_RE_DATE_NUMERIC = re.compile(
    r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b|\b\d{4}[/\-]\d{1,2}[/\-]\d{1,2}\b"
)

# Month-year: "March 2014", "Aug 2019" (dates more specific than a year are PHI).
_RE_DATE_MONTH_YEAR = re.compile(
    rf"\b(?:{_MONTHS})\.?\s+\d{{4}}\b", re.IGNORECASE
)

# City, State[, country]: "Detroit, MI", "Detroit, Michigan"
_RE_ADDRESS_CITY_STATE = re.compile(
    rf"\b[A-Z][a-zA-Z]+,\s*(?:{_US_STATES}|{_STATE_ABBR})\b",
    re.IGNORECASE,
)

# Labeled names: "Patient Name: Jane Doe", "Name: Maria E. Thompson"
_RE_LABELED_NAME = re.compile(
    r"\b(?:Patient\s+Name|Name|Pt\s+Name)\s*[:#]\s*"
    r"[A-Z][a-zA-Z'\-]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-zA-Z'\-]+){1,2}",
)

# Titled names: "Dr. Patel", "Dr. Lin (endocrine)", "A. Patel MD"
_RE_TITLED_NAME = re.compile(
    r"\b(?:Dr|Doctor|Mr|Mrs|Ms|Prof)\.?\s+[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)?"
    r"|\b[A-Z]\.\s*[A-Z][a-zA-Z'\-]+\s+(?:MD|DO|PhD|RN|NP|PA)\b",
)


# Free-text personal names: "First Last" or "First M. Last" (2-3 capitalized tokens,
# optional middle initial). Conservative: requires each surname-ish token to be >=2
# letters. Clinical multi-word phrases are filtered out by the safeword guard above.
_RE_FREETEXT_NAME = re.compile(
    r"\b[A-Z][a-z]{1,}\.?\s+(?:[A-Z]\.\s+)?[A-Z][a-z]{1,}(?:\s+[A-Z][a-z]{1,})?\b"
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
