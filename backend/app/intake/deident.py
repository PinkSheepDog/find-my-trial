"""De-identification — the HIPAA safety boundary.

This module strips HIPAA Safe Harbor-style identifiers (45 CFR 164.514(b)(2))
from chart text BEFORE it can be sent to any external service (the LLM). It is
**redaction assistance**, not certified de-identification — see
`docs/PRIVACY_DATA_FLOW.md` for the honest limits.

Design:
  * A deterministic RULE layer is ALWAYS on. It needs no models, no network,
    runs offline, and is fully testable. It covers the structured/format-bearing
    identifiers (MRN/HRN, SSN, dates, phone, email, addresses, ZIP, record IDs,
    insurance/policy numbers, device serials, URLs), facility/institution names,
    a curated US-city gazetteer plus locational phrasing, and name patterns with
    a clear signal ("Dr. X", "Nurse X", "Name: X", "Patient X", signature blocks,
    relative/contact lines, "First M. Last", or a "Firstname Lastname" whose first
    token is a known given name).
  * An optional NER layer (Presidio/spaCy) catches the residual free-text
    PERSON / LOCATION / ORGANIZATION spans the rules miss. It is additive — never
    required — and the system degrades cleanly to rule-only when Presidio is not
    installed.

CRITICAL: clinically meaningful tokens must survive de-identification. Biomarker
status ("HER2", "PD-L1", "BRCA negative"), drug names, ECOG, stage, ages-in-years,
lab values, and section/label text are NOT identifiers and must NOT be redacted.
We redact identity, not medicine. Over-redaction that destroys clinical signal is
treated as a bug, equal in severity to a leak. Every rule below is written to fail
"closed" toward keeping medicine: hyphenated clinical tokens ("PD-L1", "T-DM1",
"5-FU", "COVID-19") are structurally excluded from the record-ID rules, and city
names that collide with clinical/common English words ("Mobile", "Buffalo",
"Corona", "Jackson", "Independence") are only redacted in explicit locational
context.

Name detection is POSITIVE-SIGNAL, not denylist-based: a bare "Word Word" phrase
is only redacted as a name when it carries a name signal (middle initial, or a
first token that is a known given name). This is what keeps documents dense with
Title Case medical terms ("Serum Tumor Markers", "Chronic Liver Disease") intact.
The trade-off: an unlabeled name with an uncommon first name, no middle initial,
and no role/label context can slip through in rules-only mode — enable Presidio
(FMT_USE_PRESIDIO=true) for free-text-heavy inputs, and note that /api/match
re-scrubs as defense-in-depth.

Ages: HIPAA Safe Harbor requires ages > 89 be generalized. Ages <= 89 are retained
(they are needed for trial age-eligibility matching).
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
TAG_FACILITY = "[FACILITY]"


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
    # Structural field labels that follow "Patient"/"Mother"/"Signed by" style
    # lead-ins and must never be mistaken for the person's name.
    "id", "mrn", "hrn", "dob", "ssn", "sex", "gender", "state", "city", "zip",
    "phone", "address", "insurance", "policy", "room", "bed", "unit", "floor",
    "chart", "account", "acct", "status", "problem", "list", "note", "notes",
    "addendum", "imaging", "sites", "site", "preferences", "referral", "safety",
    "performance", "treatment", "biomarker", "deceased", "unknown", "none",
    "living", "alive", "reports", "denies", "presented", "with", "and", "or",
    # Imaging / encounter words that can follow a two-letter role abbreviation
    # ("PA Chest", "PT Evaluation") and must not be read as a surname.
    "chest", "lateral", "view", "views", "film", "portable", "upright",
    "abdomen", "pelvis", "supine", "evaluation", "eval", "therapy", "consult",
    "consultation", "progress", "discharge", "admission", "orders", "order",
    "education", "instructions", "not", "no", "infer", "return", "identify",
    # Role nouns, so "Attending Physician" / "Nurse Practitioner" are read as job
    # titles rather than as "<role> <surname>".
    "physician", "nurse", "provider", "clinician", "practitioner", "surgeon",
    "oncologist", "radiologist", "pathologist", "resident", "fellow",
    "attending", "technologist", "technician", "therapist", "pharmacist",
    "coordinator", "navigator", "staff", "team", "specialist", "assistant",
}

_NON_NAME_WORDS = _CLINICAL_SAFEWORDS | _COMMON_NON_NAME_WORDS

# Common given names (English + widely-seen international). Used as a POSITIVE
# signal: an unlabeled "Firstname Lastname" is only redacted when its first token
# is a plausible given name. This is bounded and stable, unlike enumerating all
# medical vocabulary. Not exhaustive by design — role/label rules and Presidio
# cover the long tail.
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
    # Unisex / modern given names commonly seen on charts
    "avery", "riley", "casey", "jordan", "taylor", "dana", "robin", "morgan",
    "alex", "jamie", "quinn", "reese", "rowan", "sage", "skyler", "cameron",
    "drew", "blake", "hayden", "parker", "peyton", "shannon", "kelly", "leslie",
}

# Month names so we can catch "March 2014", "Aug 2019", etc.
_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)

# US state names / common abbreviations used to detect address lines like "Detroit, MI".
# NOTE: bare state names are NOT redacted — Safe Harbor permits state-level geography,
# and the matcher needs it for site/radius preferences.
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
_STATE_WORDS = {s.replace(" ", "") for s in _US_STATES.split("|")} | set(_US_STATES.split("|"))


# ---------------------------------------------------------------------------
# US city gazetteer (approximately the 300 largest US cities plus well-known
# medical-hub cities).
#
# HONEST LIMITATION: a fixed list can never cover every US municipality, let
# alone non-US ones. It is paired with locational-phrase patterns
# ("lives in X", "travels from X for treatment") that catch off-list places, and
# with the optional Presidio LOCATION entity. Cities not on the list, not in a
# locational phrase, and not followed by a state still pass through. This is
# documented in docs/PRIVACY_DATA_FLOW.md rather than papered over.
# ---------------------------------------------------------------------------

# Cities safe to redact on sight: the token has no common clinical or everyday
# English meaning, so a bare occurrence is almost certainly geography.
_US_CITIES_UNAMBIGUOUS = {
    "Abilene", "Akron", "Albuquerque", "Alexandria", "Allentown", "Amarillo",
    "Anaheim", "Anchorage", "Ann Arbor", "Antioch", "Arlington", "Arvada",
    "Athens", "Atlanta", "Augusta", "Aurora", "Austin", "Bakersfield",
    "Baltimore", "Baton Rouge", "Beaumont", "Bellevue", "Berkeley", "Billings",
    "Birmingham", "Boise", "Boston", "Boulder", "Bridgeport", "Brockton",
    "Broken Arrow", "Brownsville", "Cambridge", "Cape Coral", "Carlsbad",
    "Carrollton", "Cary", "Cedar Rapids", "Centennial", "Chandler", "Charleston",
    "Charlotte", "Chattanooga", "Chesapeake", "Cheyenne", "Chicago",
    "Chula Vista", "Cincinnati", "Clarksville", "Clearwater", "Cleveland",
    "Clovis", "College Station", "Colorado Springs", "Columbus", "Coral Springs",
    "Corpus Christi", "Costa Mesa", "Dallas", "Dayton", "Dearborn", "Denton",
    "Denver", "Des Moines", "Detroit", "Downey", "Durham", "El Monte", "El Paso",
    "Elgin", "Elizabeth", "Elk Grove", "Escondido", "Eugene", "Evansville",
    "Everett", "Fargo", "Fayetteville", "Flint", "Fontana", "Fort Collins",
    "Fort Lauderdale", "Fort Wayne", "Fort Worth", "Fremont", "Fresno", "Frisco",
    "Fullerton", "Gainesville", "Garden Grove", "Garland", "Gilbert", "Glendale",
    "Grand Prairie", "Grand Rapids", "Greeley", "Green Bay", "Greensboro",
    "Gresham", "Hampton", "Hartford", "Hayward", "Hialeah", "High Point",
    "Hillsboro", "Hollywood", "Honolulu", "Houston", "Huntington Beach",
    "Huntsville", "Indianapolis", "Inglewood", "Irvine", "Irving",
    "Jersey City", "Joliet", "Jurupa Valley", "Kansas City", "Kent", "Killeen",
    "Knoxville", "Lafayette", "Lakeland", "Lakewood", "Lancaster", "Lansing",
    "Laredo", "Las Cruces", "Las Vegas", "League City", "Lewisville",
    "Lexington", "Lincoln", "Little Rock", "Long Beach", "Los Angeles",
    "Louisville", "Lowell", "Lubbock", "Macon", "Madison", "Manchester",
    "McAllen", "McKinney", "Memphis", "Menifee", "Meridian", "Mesa", "Mesquite",
    "Miami", "Miami Gardens", "Midland", "Milwaukee", "Minneapolis", "Miramar",
    "Modesto", "Montgomery", "Moreno Valley", "Murfreesboro", "Murrieta",
    "Nampa", "Naperville", "Nashville", "New Haven", "New Orleans", "New York",
    "Newark", "Newport News", "Norfolk", "Norman", "North Charleston",
    "North Las Vegas", "Oakland", "Oceanside", "Odessa", "Oklahoma City",
    "Olathe", "Omaha", "Ontario", "Orlando", "Overland Park", "Oxnard",
    "Palm Bay", "Palm Coast", "Palmdale", "Pasadena", "Paterson", "Pearland",
    "Pembroke Pines", "Peoria", "Philadelphia", "Phoenix", "Pittsburgh", "Plano",
    "Pomona", "Port St. Lucie", "Portland", "Providence", "Provo", "Pueblo",
    "Raleigh", "Rancho Cucamonga", "Reno", "Rialto", "Richardson", "Rio Rancho",
    "Riverside", "Rochester", "Rockford", "Roseville", "Round Rock",
    "Sacramento", "Saint Paul", "Salem", "Salinas", "Salt Lake City",
    "San Antonio", "San Bernardino", "San Diego", "San Francisco", "San Jose",
    "Santa Ana", "Santa Clara", "Santa Clarita", "Santa Maria", "Santa Rosa",
    "Savannah", "Scottsdale", "Seattle", "Shreveport", "Simi Valley",
    "Sioux Falls", "South Bend", "Spokane", "Spokane Valley", "Springfield",
    "St. Louis", "St. Paul", "St. Petersburg", "Stamford", "Sterling Heights",
    "Stockton", "Sugar Land", "Sunnyvale", "Syracuse", "Tacoma", "Tallahassee",
    "Tampa", "Temecula", "Tempe", "Thornton", "Thousand Oaks", "Toledo",
    "Topeka", "Torrance", "Tucson", "Tulsa", "Tyler", "Vallejo", "Vancouver",
    "Ventura", "Victorville", "Virginia Beach", "Visalia", "Waco", "Waterbury",
    "West Covina", "West Jordan", "West Palm Beach", "West Valley City",
    "Westminster", "Wichita", "Wilmington", "Winston-Salem", "Worcester",
    "Yonkers",
}

# Cities whose names double as ordinary English, anatomy, devices, or state names.
# Redacting these on sight would corrupt clinical text ("patient is Mobile",
# "Buffalo hump", "Jackson-Pratt drain", "Corona radiata", "functional
# Independence"), so they are redacted ONLY inside an explicit locational phrase,
# which the context rules below handle generically.
_US_CITIES_CONTEXT_ONLY = {
    "Buffalo", "Columbia", "Concord", "Corona", "Gary", "Henderson",
    "Independence", "Jackson", "Mobile", "Orange", "Reading", "Richmond",
    "Sparks", "Surprise", "Union", "Victoria", "Warren", "Washington",
}


def _city_alternation(cities: set[str]) -> str:
    """Longest-first alternation so "Kansas City" wins over "Kansas"."""
    return "|".join(re.escape(c) for c in sorted(cities, key=lambda c: (-len(c), c)))


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
    """Rule-based de-identifier (always on) with optional Presidio NER augmentation.

    `presidio_analyzer` exists purely so the NER code path can be unit-tested with a
    stub analyzer on machines where Presidio is not installed.
    """

    def __init__(self, use_presidio: bool = False, presidio_analyzer=None) -> None:
        if presidio_analyzer is not None:
            self._presidio = presidio_analyzer
        else:
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
        # MRN BEFORE SSN: an explicitly labeled medical-record number that happens to
        # be formatted like an SSN must be tagged [MRN], not [SSN].
        out = sub(_RE_MRN, TAG_MRN, out)             # labeled MRN/HRN (numeric or alphanumeric)
        out = sub(_RE_SSN, TAG_SSN, out)
        out = sub(_RE_PHONE, TAG_PHONE, out)
        out = self._redact_labeled_phone(out, counts)
        out = self._redact_insurance_and_device(out, counts)
        out = sub(_RE_NCT_OTHER_ID, TAG_ID, out)     # patient-side record IDs (P-1001 etc.)
        out = self._redact_record_ids(out, counts)   # SYNTH-LUNG-003, BCBS-772819, ...
        out = self._redact_ages_over_89(out, counts)
        out = sub(_RE_DOB_LABELED, TAG_DATE, out)
        out = sub(_RE_DATE_NUMERIC, TAG_DATE, out)
        out = sub(_RE_DATE_MONTH_YEAR, TAG_DATE, out)
        out = sub(_RE_DATE_TEXT, TAG_DATE, out)      # "May 12, 2026", "12 May 2026"
        out = sub(_RE_LONG_NUMERIC_ID, TAG_ID, out)  # bare long digit runs (barcodes/record #s)
        out = sub(_RE_STREET, TAG_ADDRESS, out)      # "123 Main St"
        out = sub(_RE_ZIP, TAG_ADDRESS, out)         # ZIP / ZIP+4 / labeled ZIP

        # Facilities BEFORE cities so "Cleveland Clinic" is one [FACILITY] and not
        # "[ADDRESS] Clinic".
        out = self._redact_facilities(out, counts)

        # Names BEFORE cities so "referred from Dr. Patel" is a [NAME], and the
        # locational rules never see a bare title to mistake for a place.
        out = sub(_RE_LABELED_NAME, TAG_NAME, out)
        out = self._redact_role_names(out, counts)   # Dr./Nurse/RN/Attending/...
        out = self._redact_lead_in_names(out, counts)  # Patient X, signed by X, Mother: X
        out = self._redact_freetext_names(out, counts)

        # Geography, smallest-to-largest specificity.
        out = self._redact_city_state(out, counts)
        out = sub(_RE_CITY_BARE, TAG_ADDRESS, out)
        out = self._redact_cities_in_context(out, counts)
        out = sub(_RE_ZIP_AFTER_ADDRESS, TAG_ADDRESS, out)

        # --- Optional NER pass for residual free-text person/place/org spans ---
        if self._presidio is not None:
            out, ner_counts = self._presidio_entities(out)
            for tag, n in ner_counts.items():
                counts[tag] = counts.get(tag, 0) + n

        return DeidResult(text=out, redaction_counts=counts)

    # ------------------------------------------------------------------ ages --
    @staticmethod
    def _redact_ages_over_89(text: str, counts: dict[str, int]) -> str:
        """HIPAA generalizes ages > 89. Ages <= 89 are RETAINED — the matcher needs
        them for trial age-eligibility. Three surface forms are handled: a unit
        suffix ("92 year old", "95-year-old"), a label ("Age: 93"), and a copula
        ("The patient is 95")."""

        def _suffix_repl(m: re.Match[str]) -> str:
            if int(m.group("age")) > 89:
                counts[TAG_AGE] = counts.get(TAG_AGE, 0) + 1
                return TAG_AGE + m.group("suffix")
            return m.group(0)

        def _bare_repl(m: re.Match[str]) -> str:
            if int(m.group("age")) > 89:
                counts[TAG_AGE] = counts.get(TAG_AGE, 0) + 1
                return m.group("lead") + TAG_AGE
            return m.group(0)

        text = _RE_AGE.sub(_suffix_repl, text)
        text = _RE_AGE_LABELED.sub(_bare_repl, text)
        text = _RE_AGE_COPULA.sub(_bare_repl, text)
        return text

    # ------------------------------------------------------- record identifiers --
    @staticmethod
    def _redact_record_ids(text: str, counts: dict[str, int]) -> str:
        """Local/site record IDs copied through a chart ("SYNTH-LUNG-003"), including
        the repeated-in-footer case. Structurally excludes clinical hyphen tokens:
        the multi-segment form needs >= 3 ALL-CAPS segments (so "PD-L1", "T-DM1",
        "CTLA-4" cannot match) and the two-segment form needs a >= 4-digit tail
        (so "COVID-19", "IL-6" cannot match)."""

        def _repl(m: re.Match[str]) -> str:
            token = m.group(0)
            if not any(ch.isdigit() for ch in token):
                return token  # "NOT-FOR-CLINICAL" style text, not an ID
            segments = [s.lower() for s in token.split("-")]
            if all(s in _NON_NAME_WORDS for s in segments):
                return token
            counts[TAG_ID] = counts.get(TAG_ID, 0) + 1
            return TAG_ID

        return _RE_RECORD_ID.sub(_repl, text)

    @staticmethod
    def _redact_labeled_phone(text: str, counts: dict[str, int]) -> str:
        """A labeled contact number that is not in strict NANP form still leaks a
        phone number ("Phone: (555) 0100"). Contextual on the label so lab values
        are untouched."""

        def _repl(m: re.Match[str]) -> str:
            counts[TAG_PHONE] = counts.get(TAG_PHONE, 0) + 1
            return m.group("lead") + TAG_PHONE

        return _RE_LABELED_PHONE.sub(_repl, text)

    @staticmethod
    def _redact_insurance_and_device(text: str, counts: dict[str, int]) -> str:
        """Health-plan beneficiary numbers and device identifiers/serials — both are
        named HIPAA identifier classes. Contextual (keyword + adjacent identifier
        token) so ordinary numbers are untouched."""

        def _repl(m: re.Match[str]) -> str:
            counts[TAG_ID] = counts.get(TAG_ID, 0) + 1
            return m.group("lead") + TAG_ID

        return _RE_INSURANCE_DEVICE.sub(_repl, text)

    # ------------------------------------------------------------- facilities --
    @staticmethod
    def _redact_facilities(text: str, counts: dict[str, int]) -> str:
        """Institution names: the capitalized run that ends in an institution keyword
        ("Mercy General Hospital", "Cleveland Clinic Taussig Cancer Institute")."""

        def _repl(m: re.Match[str]) -> str:
            counts[TAG_FACILITY] = counts.get(TAG_FACILITY, 0) + 1
            return TAG_FACILITY

        return _RE_FACILITY.sub(_repl, text)

    # ------------------------------------------------------------------ names --
    @staticmethod
    def _name_tokens_ok(name: str) -> bool:
        """A candidate name span is only a person name if no token is a known
        structural/clinical word."""
        tokens = [t for t in re.split(r"[ \t.]+", name) if t]
        if not tokens:
            return False
        return not any(t.lower() in _NON_NAME_WORDS for t in tokens)

    @classmethod
    def _redact_role_names(cls, text: str, counts: dict[str, int]) -> str:
        """Names introduced by a clinical role or honorific: "Dr. Patel",
        "Nurse Alvarez", "Attending Rodriguez", "A. Okafor MD"."""

        def _repl(m: re.Match[str]) -> str:
            name = m.groupdict().get("name")
            if name is None:                     # trailing-credential form ("A. Okafor MD")
                counts[TAG_NAME] = counts.get(TAG_NAME, 0) + 1
                return TAG_NAME
            if not cls._name_tokens_ok(name):
                return m.group(0)
            counts[TAG_NAME] = counts.get(TAG_NAME, 0) + 1
            return m.group("lead") + TAG_NAME

        text = _RE_HONORIFIC_NAME.sub(_repl, text)
        text = _RE_ROLE_NAME.sub(_repl, text)
        text = _RE_CREDENTIALED_NAME.sub(_repl, text)
        return text

    @classmethod
    def _redact_lead_in_names(cls, text: str, counts: dict[str, int]) -> str:
        """Names introduced by a non-honorific lead-in, all of which the audit found
        leaking: the colon-less "Patient Maria Gonzalez", signature blocks
        ("Electronically signed by Sarah Chen"), and relatives / emergency contacts
        ("Mother: Jane Doe", "Next of kin - Robert Ellis")."""

        def _repl(m: re.Match[str]) -> str:
            name = m.group("name")
            if not cls._name_tokens_ok(name):
                return m.group(0)
            counts[TAG_NAME] = counts.get(TAG_NAME, 0) + 1
            return m.group("lead") + TAG_NAME

        for pattern in (_RE_SIGNATURE_NAME, _RE_RELATIVE_NAME,
                        _RE_RELATIVE_NAME_BARE, _RE_PATIENT_NAME):
            text = pattern.sub(_repl, text)
        return text

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

    # --------------------------------------------------------------- geography --
    @staticmethod
    def _redact_city_state(text: str, counts: dict[str, int]) -> str:
        """"City, State" lines. A leading token that is itself a state is a list of
        states ("Prefers Michigan, Ohio"), not a city — Safe Harbor permits state, and
        the matcher needs those site preferences, so those are left intact."""

        def _repl(m: re.Match[str]) -> str:
            head = m.group(0).split(",", 1)[0].strip().lower()
            if head in _STATE_WORDS:
                return m.group(0)
            counts[TAG_ADDRESS] = counts.get(TAG_ADDRESS, 0) + 1
            return TAG_ADDRESS

        return _RE_ADDRESS_CITY_STATE.sub(_repl, text)

    @staticmethod
    def _redact_cities_in_context(text: str, counts: dict[str, int]) -> str:
        """Places named in an explicit locational phrase ("lives in X", "resides in X",
        "travels from X for treatment"). This is what covers municipalities that are
        not in the gazetteer, and the ambiguous city names that are unsafe to redact
        on sight. States are deliberately NOT redacted (Safe Harbor permits state)."""

        def _repl(m: re.Match[str]) -> str:
            place = m.group("city")
            tokens = [t for t in re.split(r"[ \t]+", place) if t]
            low = [t.lower() for t in tokens]
            if any(t in _NON_NAME_WORDS or t in _CITY_LEADIN_STOPWORDS for t in low):
                return m.group(0)
            if " ".join(low) in _STATE_WORDS or low[0] in _STATE_WORDS:
                return m.group(0)   # a state is permitted geography
            counts[TAG_ADDRESS] = counts.get(TAG_ADDRESS, 0) + 1
            return m.group("lead") + TAG_ADDRESS

        for pattern in (_RE_CITY_CONTEXT, _RE_CITY_TRAVEL_PURPOSE):
            text = pattern.sub(_repl, text)
        return text

    # ------------------------------------------------------------- optional NER --
    def _presidio_entities(self, text: str) -> tuple[str, dict[str, int]]:
        """Optional Presidio pass over PERSON, LOCATION and ORGANIZATION.

        The earlier implementation asked only for PERSON, which meant the layer the
        docs advertised as the mitigation for free-text cities could not possibly
        catch them. Spans are rewritten here rather than via presidio_anonymizer so
        the path has no second optional dependency and can be exercised in tests
        with a stub analyzer.
        """
        analyzer = self._presidio
        try:
            results = analyzer.analyze(
                text=text, entities=list(_PRESIDIO_ENTITIES), language="en"
            )
        except Exception:
            # NER is best-effort; never let it break the always-on rule layer.
            return text, {}

        try:
            spans: list[tuple[int, int, str]] = []
            for r in results or []:
                tag = _PRESIDIO_ENTITY_TAGS.get(getattr(r, "entity_type", ""))
                if tag is None or float(getattr(r, "score", 0.0)) < _PRESIDIO_MIN_SCORE:
                    continue
                start, end = int(r.start), int(r.end)
                if start < 0 or end > len(text) or end <= start:
                    continue
                spans.append((start, end, tag))

            spans.sort(key=lambda s: (s[0], -s[1]))
            pieces: list[str] = []
            counts: dict[str, int] = {}
            cursor = 0
            for start, end, tag in spans:
                if start < cursor:
                    continue                       # overlaps an already-rewritten span
                fragment = text[start:end]
                if "[" in fragment or "]" in fragment:
                    continue                       # never rewrite an emitted tag
                pieces.append(text[cursor:start])
                pieces.append(tag)
                counts[tag] = counts.get(tag, 0) + 1
                cursor = end
            pieces.append(text[cursor:])
            return "".join(pieces), counts
        except Exception:
            return text, {}


# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------
_RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

_RE_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

_RE_PHONE = re.compile(
    r"(?:\+?1[\s.\-]?)?(?:\(\d{3}\)\s*|\d{3}[\s.\-])\d{3}[\s.\-]\d{4}\b"
)

# Labeled contact numbers that are not in strict NANP form, e.g. "Phone: (555) 0100".
# The label is required so lab values and doses can never match.
_RE_LABELED_PHONE = re.compile(
    r"(?P<lead>\b(?i:phone|telephone|tel|mobile|cell|fax|pager|beeper)"
    r"[ \t]*(?:number|no\.?|#)?[ \t]*[:#]?[ \t]*)"
    r"(?P<val>\+?\d{0,2}[ \t]*\(?\d{3}\)?[ \t.\-]*\d{3,4}(?:[ \t.\-]*\d{4})?)\b"
)

# MRN / HRN / medical-record numbers. Labeled, value may be numeric OR alphanumeric
# ("BRE00000273", "SYNTH-0000-HER2"), and the value may sit on the next line
# (tabular reports). Crosses at most one line break to reach the value.
_RE_MRN = re.compile(
    r"\b(?:MRN|HRN|Medical\s+Record(?:\s+Number)?|Hospital\s+Record(?:\s+Number)?|"
    r"Record\s*(?:Number|No\.?|#))"
    r"[ \t]*[:#]?[ \t]*\n?[ \t]*"
    r"(?:[A-Za-z]{1,6}[\-]?)?\d[A-Za-z0-9\-]{3,}\b",
    re.IGNORECASE,
)

# Patient-side record identifiers like "P-1001", "Patient ID: P-1001".
# Deliberately excludes NCT IDs (those are public trial IDs, not PHI).
_RE_NCT_OTHER_ID = re.compile(
    r"\b(?:Patient\s*ID|Pt\s*ID|Account|Acct)\s*[:#]?\s*[A-Za-z]?-?\d{3,}\b"
    r"|\bP-\d{3,}\b",
    re.IGNORECASE,
)

# Local/site record IDs. Two safe shapes only:
#   * >= 3 ALL-CAPS hyphen segments  -> "SYNTH-LUNG-003", "SYNTH-BREAST-HER2-001"
#   * ALL-CAPS prefix + >= 4 digits  -> "BCBS-772819", "DX-99182"
# Both shapes are unreachable for "PD-L1", "T-DM1", "CTLA-4", "5-FU", "COVID-19".
# NCT trial IDs are public, not PHI, and are explicitly excluded.
_RE_RECORD_ID = re.compile(
    r"\b(?!NCT\b)(?:[A-Z][A-Z0-9]+(?:-[A-Z0-9]+){2,}|[A-Z]{2,6}-\d{4,})\b"
)

# Insurance / health-plan / device identifiers, contextual on the label keyword.
# The value needs a digit and >= 4 trailing characters, so "group 3" or "lot A"
# cannot trip it.
_RE_INSURANCE_DEVICE = re.compile(
    r"(?P<lead>\b(?:policy|member|group|subscriber|insurance|payer|plan|"
    r"serial|device|implant|lot|catalog)"
    r"[ \t]*(?:number|numbers|no\.?|num|id|ids|#)?[ \t]*[:#]?[ \t]*)"
    r"(?P<val>[A-Za-z]{0,6}-?\d[A-Za-z0-9\-]{3,})\b",
    re.IGNORECASE,
)

# Age with a unit suffix, e.g. "92 year old", "95-year-old", "93 yo", "91 yrs".
# The suffix is captured so it can be re-attached after the tag.
_RE_AGE = re.compile(
    r"\b(?P<age>\d{1,3})(?P<suffix>[\s\-]*(?:year[s]?[- ]old|y/?o|yo|yrs?[- ]old|yrs))\b",
    re.IGNORECASE,
)

# Labeled age with no unit: "Age: 93", "Age 93", "AGE=93".
_RE_AGE_LABELED = re.compile(
    r"(?P<lead>\b(?:age|aged)[ \t]*[:=#]?[ \t]*)(?P<age>\d{1,3})\b"
    r"(?![ \t]*(?:%|mg|ml|kg|cm|mm|mcg|mmol|meq|/|\.\d))",
    re.IGNORECASE,
)

# Copula age with no unit: "The patient is 95.", "she is 93".
_RE_AGE_COPULA = re.compile(
    r"(?P<lead>\b(?:patient|pt|male|female|man|woman|gentleman|lady|she|he)[ \t]+"
    r"is[ \t]+(?:a[ \t]+|an[ \t]+)?)(?P<age>\d{1,3})\b"
    r"(?![ \t]*(?:%|mg|ml|kg|cm|mm|mcg|mmol|meq|/|\.\d))",
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
# Deliberately single-line ([ \t], never \s) and case-SENSITIVE on the suffix. The
# previous case-insensitive, newline-crossing version destroyed clinical text: in
# "...is the 2026 lung core.\n\nCT 06/28/26..." it matched "2026 lung core.\n\nCT"
# as "<number> <words> Ct" and redacted the CT scan away. Real chart addresses are
# Title-Cased; lowercase street lines are a documented miss, not a silent one.
_RE_STREET = re.compile(
    r"\b\d{1,6}[ \t]+(?:[A-Za-z][A-Za-z0-9.'\-]*[ \t]+){0,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|"
    r"Way|Place|Pl|Terrace|Ter|Circle|Cir|Highway|Hwy|Parkway|Pkwy|Suite|Ste|Apt)\b\.?"
)

# ZIP: unambiguous ZIP+4 anywhere; a bare 5-digit only right after a US state or a
# ZIP/postal label. A context-free 5-digit number is NOT treated as a ZIP — see the
# "known limitations" section of docs/PRIVACY_DATA_FLOW.md for why.
# The optional "-\d{4}" tail is on EVERY alternative: alternation is leftmost-first,
# so a labeled "ZIP 48201-1234" used to match the label branch and leave "-1234"
# dangling in the output.
_RE_ZIP = re.compile(
    rf"\b\d{{5}}-\d{{4}}\b"
    rf"|(?<=\b(?:{_STATE_ABBR})\s)\d{{5}}(?:-\d{{4}})?\b"
    rf"|(?i:zip|postal)\s*(?:code)?\s*[:#]?\s*\d{{5}}(?:-\d{{4}})?\b"
)

# A 5-digit number immediately after a redacted place is a ZIP by context.
_RE_ZIP_AFTER_ADDRESS = re.compile(r"(?<=\[ADDRESS\][ \t])\d{5}\b")

# Bare long digit runs (>=9 digits): barcodes, document control numbers, record IDs.
# Specific formats (phone, SSN, MRN, dates) are handled above and run first.
_RE_LONG_NUMERIC_ID = re.compile(r"\b\d{9,}\b")

# City, State[, country]: "Detroit, MI", "Detroit, Michigan".
# Case-SENSITIVE: the previous re.IGNORECASE version matched ordinary prose —
# "recommend treatment, or predict outcome" parsed as "<City>, OR" (Oregon) and
# redacted the word "treatment". The city token must be Title-Cased and a bare
# state abbreviation must be upper-case.
_RE_ADDRESS_CITY_STATE = re.compile(
    rf"\b[A-Z][a-zA-Z]+,[ \t]*(?:(?i:{_US_STATES})|(?:{_STATE_ABBR}))\b"
)

# Bare gazetteer city, matched case-sensitively so "mobile"/"reading" prose is safe.
# Not preceded/followed by a hyphen, so eponymous device names ("Jackson-Pratt")
# and hyphenated compounds are left alone.
_RE_CITY_BARE = re.compile(
    rf"(?<![\w\-])(?:{_city_alternation(_US_CITIES_UNAMBIGUOUS)})(?![\w\-])"
)

# Institution keywords, longest-first so "Cancer Center" beats a bare "Center".
# A bare "Center"/"Centre" is deliberately NOT a keyword — too collision-prone.
_FACILITY_KEYWORD = (
    r"Cancer\s+Center|Cancer\s+Centre|Cancer\s+Institute|Cancer\s+Hospital|"
    r"Medical\s+Center|Medical\s+Centre|Medical\s+Group|Medical\s+Clinic|"
    r"Health\s+System|Health\s+Center|Health\s+Centre|Health\s+Network|"
    r"Healthcare\s+System|Hospital\s+System|"
    r"Oncology\s+Center|Oncology\s+Centre|Oncology\s+Institute|Oncology\s+Group|"
    r"Surgery\s+Center|Surgical\s+Center|Imaging\s+Center|Infusion\s+Center|"
    r"Treatment\s+Center|Rehabilitation\s+Center|Research\s+Institute|"
    r"Nursing\s+Home|Hospice|Hospitals|Hospital|Clinics|Clinic|Infirmary|"
    r"Institute|Sanatorium|Polyclinic|Laboratories"
)

# A run of capitalized words (optionally joined by "of"/"the"/"and") ending in an
# institution keyword: "Mercy General Hospital", "University of Michigan Health
# System", "Cleveland Clinic Taussig Cancer Institute". The leading run is greedy
# so the longest institution name wins.
_RE_FACILITY = re.compile(
    rf"\b(?:[A-Z][A-Za-z'&.\-]*[ \t]+(?:of[ \t]+|the[ \t]+|and[ \t]+)?){{1,5}}"
    rf"(?:{_FACILITY_KEYWORD})\b"
)

# Labeled names: "Patient Name: Jane Doe", "Name: Maria E. Thompson". The label and
# value may be on separate lines (crosses at most one line break); the name tokens
# themselves stay on a single line so we don't swallow the next line's content.
_RE_LABELED_NAME = re.compile(
    r"\b(?:Patient\s+Name|Name|Pt\s+Name)[ \t]*[:#][ \t]*\n?[ \t]*"
    r"[A-Z][a-zA-Z'\-]+(?:[ \t]+[A-Z]\.?(?![A-Za-z]))?(?:[ \t]+[A-Z][a-zA-Z'\-]+){1,2}",
)

# A person-name span: "Patel", "Maria Gonzalez", "Maria E. Thompson".
# The optional middle initial must NOT be followed by more letters, otherwise
# "Maria Gonzalez" would be split into "Maria G" + a dangling "onzalez".
_NAME_SPAN = (
    r"[A-Z][a-zA-Z'\-]+(?:[ \t]+[A-Z]\.?(?![A-Za-z]))?"
    r"(?:[ \t]+[A-Z][a-zA-Z'\-]+){0,2}"
)

# Honorifics and clinical roles that introduce a person's name. Case-sensitive on
# purpose: "treating oncologist coordinates" (lowercase) is prose, not a name intro.
# Deliberately EXCLUDED: "Sister" (the Sister Mary Joseph nodule is a physical-exam
# finding, not a person), and the bare credentials MD/DO/MA/RT/PT/OT/SLP — those are
# either suffixes (handled by _RE_CREDENTIALED_NAME) or collide with clinical
# abbreviations ("DO NOT", "PT eval", "MA" for medical assistant).
# Personal honorifics. Nothing but a person's name follows these, so the
# "is this token a clinical word?" guard is NOT applied to them — otherwise a
# referring physician named "Dr. Sample" survives the scrub because "sample" is a
# lab word.
_HONORIFICS = r"Dr|Doctor|Mr|Mrs|Ms|Miss|Mx|Prof|Professor"

# Clinical roles. These DO get the guard, because a role word is often followed by
# another clinical noun ("Attending Physician", "PA Chest", "Nurse Practitioner").
_ROLE_TITLES = (
    r"Nurse|Midwife|"
    r"Attending|Consultant|Surgeon|Oncologist|Radiologist|Pathologist|"
    r"Anesthesiologist|Cardiologist|Physician|Provider|Clinician|Practitioner|"
    r"Technologist|Technician|Therapist|Pharmacist|Dietitian|Counselor|"
    r"Resident|Fellow|Intern|Coordinator|Navigator|Phlebotomist|Sonographer|"
    r"RN|LPN|NP|APRN|PA|CRNA|PharmD"
)

_ALL_TITLES = rf"{_HONORIFICS}|{_ROLE_TITLES}"

# Honorific + name: redacted unconditionally (strong signal, no word guard).
_RE_HONORIFIC_NAME = re.compile(
    rf"\b(?:{_HONORIFICS})\.?[ \t]+[A-Z][a-zA-Z'\-]+"
    rf"(?:[ \t]+[A-Z]\.?(?![A-Za-z]))?(?:[ \t]+[A-Z][a-zA-Z'\-]+)?"
)

# Role + name: redacted only when the following tokens are not clinical words.
_RE_ROLE_NAME = re.compile(
    rf"(?P<lead>\b(?:{_ROLE_TITLES})\.?[ \t]*[:\-]?[ \t]+)(?P<name>{_NAME_SPAN})"
)

# Trailing-credential form: "A. Okafor MD", "J. Smith, PhD".
_RE_CREDENTIALED_NAME = re.compile(
    r"\b[A-Z]\.[ \t]*[A-Z][a-zA-Z'\-]+[ \t]*,?[ \t]*(?:MD|DO|PhD|RN|NP|PA|PharmD|DNP)\b"
)

# Signature blocks: "Electronically signed by Sarah Chen, MD", "/s/ Maria Lopez".
_RE_SIGNATURE_NAME = re.compile(
    r"(?P<lead>(?:\b(?i:electronically\s+signed\s+by|e-?signed\s+by|signed\s+by|"
    r"authenticated\s+by|attested\s+by|dictated\s+by|transcribed\s+by|"
    r"reviewed\s+by|entered\s+by)|/s/)[ \t]*[:\-]?[ \t]*"
    rf"(?:{_ALL_TITLES})?\.?[ \t]*)(?P<name>{_NAME_SPAN})"
)

# Relatives / emergency contacts — a HIPAA identifier class in their own right.
# NOTE the leading \b: without it "son" matched inside "Jackson-Pratt" and turned a
# surgical drain into "Jackson-[NAME]".
# Two forms: relationship words that are safe without a separator ("Daughter Emily
# Watson"), and the rest, which need an explicit ":"/","/"-" so that ordinary prose
# ("Sister Mary Joseph nodule", "brother had colon cancer") is untouched.
_RELATIVE_TERMS_BARE = (
    r"mother|father|spouse|husband|wife|daughter|son|parent|guardian|"
    r"caregiver|caretaker|grandmother|grandfather|aunt|uncle|niece|nephew"
)
_RELATIVE_TERMS_LABELED = (
    rf"{_RELATIVE_TERMS_BARE}|sister|brother|partner|cousin|proxy|surrogate|"
    r"next\s+of\s+kin|emergency\s+contact|contact"
)
_RE_RELATIVE_NAME = re.compile(
    rf"(?P<lead>\b(?i:{_RELATIVE_TERMS_LABELED})"
    rf"(?:'s)?[ \t]*(?:name)?[ \t]*[:\-,][ \t]*)(?P<name>{_NAME_SPAN})"
)
_RE_RELATIVE_NAME_BARE = re.compile(
    rf"(?P<lead>\b(?i:{_RELATIVE_TERMS_BARE})(?:'s)?[ \t]+)"
    r"(?P<name>[A-Z][a-zA-Z'\-]+(?:[ \t]+[A-Z]\.?(?![A-Za-z]))?"
    r"[ \t]+[A-Z][a-zA-Z'\-]+)"
)

# The colon-less "Patient Maria Gonzalez" construction (and "pt Avery Sample").
# The trailing token filter in _name_tokens_ok keeps "Patient ID", "Patient
# Preferences" and similar structural phrases intact.
_RE_PATIENT_NAME = re.compile(
    rf"(?P<lead>(?i:\bpatient|\bpt|\bsubject|\bclient)[ \t]*:?[ \t]+)(?P<name>{_NAME_SPAN})"
)

# Free-text personal names: "First Last" or "First M. Last" (2-3 capitalized tokens,
# optional middle initial), on a SINGLE line. Whether it is actually redacted is
# decided in _redact_freetext_names (given-name / middle-initial signal). The "mi"
# group flags the middle-initial form, a strong name signal.
_RE_FREETEXT_NAME = re.compile(
    r"\b[A-Z][a-z]{1,}[ \t]+(?:(?P<mi>[A-Z])\.[ \t]+)?[A-Z][a-z]{1,}"
    r"(?:[ \t]+[A-Z][a-z]{1,})?\b"
)

# Locational lead-ins that mark the following proper noun as a place. These catch
# municipalities that are not in the gazetteer.
_LOCATION_LEADINS = (
    r"lives?\s+in|living\s+in|resides?\s+in|residing\s+in|relocated\s+to|"
    r"relocating\s+to|moved\s+to|moving\s+to|travels?\s+from|travell?ing\s+from|"
    r"commutes?\s+from|drives?\s+from|transferred\s+from|transferring\s+from|"
    r"referred\s+from|based\s+in|located\s+in|resident\s+of|residence\s+in|"
    r"hometown\s+of|native\s+of|home\s+in|returns?\s+to|relocated\s+from"
)

_RE_CITY_CONTEXT = re.compile(
    rf"(?P<lead>\b(?i:{_LOCATION_LEADINS})[ \t]+)"
    r"(?P<city>[A-Z][a-zA-Z'\-]+(?:[ \t]+[A-Z][a-zA-Z'\-]+){0,2})\b"
)

# "from <Place> for treatment/care/infusions/..." — the travel-for-care phrasing.
_RE_CITY_TRAVEL_PURPOSE = re.compile(
    r"(?P<lead>\bfrom[ \t]+)"
    r"(?P<city>[A-Z][a-zA-Z'\-]+(?:[ \t]+[A-Z][a-zA-Z'\-]+){0,2})"
    r"(?=[ \t]+for[ \t]+(?:treatment|care|therapy|chemo|chemotherapy|radiation|"
    r"surgery|infusions?|appointments?|visits?|evaluation|consultation|"
    r"follow-?up|the[ \t]+trial|trial|study)\b)"
)

# Tokens that may follow a locational lead-in but are never a place name.
_CITY_LEADIN_STOPWORDS = {
    "dr", "doctor", "mr", "mrs", "ms", "miss", "prof", "professor", "nurse",
    "his", "her", "their", "our", "the", "this", "that", "an", "another",
    "outside", "home", "there", "here", "abroad", "overseas",
}


# ---------------------------------------------------------------------------
# Optional Presidio NER layer
# ---------------------------------------------------------------------------
# The docs advertise this layer as the mitigation for free-text places and
# institutions, so it must request those entity types — not PERSON alone.
_PRESIDIO_ENTITIES = ("PERSON", "LOCATION", "ORGANIZATION")
_PRESIDIO_ENTITY_TAGS = {
    "PERSON": TAG_NAME,
    "LOCATION": TAG_ADDRESS,
    "GPE": TAG_ADDRESS,
    "ORGANIZATION": TAG_FACILITY,
    "ORG": TAG_FACILITY,
}
_PRESIDIO_MIN_SCORE = 0.6


def _try_load_presidio():  # pragma: no cover - depends on optional install
    """Return a Presidio AnalyzerEngine, or None when Presidio is not installed.
    Only the analyzer is needed — span rewriting is done in-module so there is no
    dependency on presidio_anonymizer."""
    try:
        from presidio_analyzer import AnalyzerEngine
        return AnalyzerEngine()
    except Exception:
        return None


_default: Deidentifier | None = None


def deidentify(text: str, use_presidio: bool = False) -> DeidResult:
    """Module-level convenience wrapper using a cached default de-identifier."""
    global _default
    if _default is None:
        _default = Deidentifier(use_presidio=use_presidio)
    return _default.deidentify(text)
