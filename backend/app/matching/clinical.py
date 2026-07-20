"""Clinical normalization used as GATES before biomarker/semantic overlap is rewarded.

The prior ranking let a wrong-primary-cancer or an imaging/registry study outrank the
right treatment trial because biomarker/lexical overlap dominated. This module gives
retrieval the hard signals it was missing:

  * disease_families_of(text) -> the cancer family/families named in a trial's
    conditions or a patient's diagnosis (breast, lung, pancreatic, ...). Retrieval
    rejects trials whose family is disjoint from the patient's, unless the trial is a
    tumour-agnostic / biomarker basket. Trials whose text names NO recognized family
    are no longer exempt from the gate — see `is_oncology_text`.
  * primary_purpose(study_type, study_design) -> treatment | diagnostic | screening |
    prevention | supportive_care | observational | ... parsed from the registry's
    "Primary Purpose" field. When treatment studies are requested, non-treatment
    purposes are dropped. An INTERVENTIONAL study with NO stated purpose is
    "unknown" (conservative) and `refine_purpose` then tries to infer imaging /
    screening / registry / natural-history intent from the title + summary.
  * evidence helpers (`snippet_for`, `grounded_source`) so every reason and caution
    the UI shows can carry a VERBATIM quote from the trial record, and so an LLM's
    claimed evidence can be verified against the record before it is displayed.
  * eligibility conflict detection (`eligibility_conflicts`) — the canonical
    "patient has a condition the trial excludes" case, read out of the trial's own
    exclusion language and checked against comorbidities / organ flags / ECOG.

Deterministic and offline; no model calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Ordered so more specific families win when keywords overlap (e.g. gastroesophageal).
# Each family -> substrings that, if present, imply that cancer family.
_DISEASE_FAMILY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "breast cancer": ("breast", "ductal carcinoma", "lobular carcinoma", "dcis", "tnbc",
                      "triple-negative breast", "triple negative breast"),
    "non-small cell lung cancer": ("nsclc", "non-small cell", "non small cell",
                                   "lung adenocarcinoma", "lung squamous", "lung cancer", "pulmonary carcinoma"),
    "small cell lung cancer": ("small cell lung", "sclc"),
    "pancreatic cancer": ("pancrea",),
    "colorectal cancer": ("colorectal", "colon cancer", "rectal cancer", "colon adenocarcinoma"),
    "prostate cancer": ("prostate",),
    "ovarian cancer": ("ovarian", "fallopian tube", "primary peritoneal"),
    "melanoma": ("melanoma",),
    "gastric cancer": ("gastric", "stomach cancer", "gastroesophageal", "gastro-esophageal", "esophagogastric"),
    "esophageal cancer": ("esophageal", "oesophageal", "esophagus"),
    "bladder cancer": ("bladder", "urothelial"),
    "renal cell carcinoma": ("renal cell", "kidney cancer", "renal cancer"),
    "hepatocellular carcinoma": ("hepatocellular", "liver cancer"),
    "biliary tract cancer": ("cholangiocarcinoma", "biliary", "gallbladder", "bile duct"),
    "head and neck cancer": ("head and neck", "oropharyng", "laryng", "nasopharyng", "hypopharyng"),
    "lymphoma": ("lymphoma", "hodgkin"),
    "leukemia": ("leukemia", "leukaemia", "myelodysplastic"),
    "multiple myeloma": ("myeloma",),
    "glioma": ("glioblastoma", "glioma", "astrocytoma", "gbm"),
    "cervical cancer": ("cervical cancer", "cervix"),
    "endometrial cancer": ("endometrial", "uterine"),
    "sarcoma": ("sarcoma",),
    "thyroid cancer": ("thyroid cancer", "thyroid carcinoma"),
    # --- coverage expansion: families the 23-family table missed entirely, each of
    # which previously left a trial UNCLASSIFIED and therefore exempt from the gate.
    "neuroendocrine tumor": ("neuroendocrine", "carcinoid", "pheochromocytoma", "paraganglioma"),
    "mesothelioma": ("mesothelioma",),
    "germ cell tumor": ("germ cell", "testicular cancer", "testicular carcinoma", "seminoma"),
    "anal cancer": ("anal cancer", "anal canal", "anal carcinoma", "anal squamous"),
    "non-melanoma skin cancer": ("basal cell carcinoma", "cutaneous squamous cell", "merkel cell"),
    "thymic cancer": ("thymoma", "thymic carcinoma"),
    "salivary gland cancer": ("salivary gland", "adenoid cystic"),
    "cns tumor": ("medulloblastoma", "ependymoma", "meningioma", "brain tumor",
                  "brain tumour", "brain neoplasm", "craniopharyngioma"),
    "neuroblastoma": ("neuroblastoma",),
    "wilms tumor": ("wilms", "nephroblastoma"),
    "vulvar or vaginal cancer": ("vulvar", "vaginal cancer"),
    "penile cancer": ("penile cancer", "penile carcinoma"),
    "small bowel cancer": ("small bowel adenocarcinoma", "duodenal adenocarcinoma"),
    "myeloproliferative neoplasm": ("myelofibrosis", "polycythemia vera", "essential thrombocythemia",
                                    "myeloproliferative"),
    "cancer of unknown primary": ("unknown primary", "occult primary"),
}

# Generic oncology language. A trial with NO recognized disease family but which is
# clearly an oncology study ("advanced neoplasms", "solid malignancies") is kept and
# FLAGGED as unclassified. A trial with neither is not an oncology study at all and is
# rejected for a patient who has a cancer family — previously both sailed through the
# gate untouched because the gate only ran when `rec.disease_families` was non-empty.
_ONCOLOGY_MARKERS = (
    "cancer", "carcinoma", "tumor", "tumour", "neoplas", "malignan", "oncolog",
    "metasta", "sarcoma", "leukem", "leukaem", "lymphom", "myelom", "blastoma",
    "adenocarcinoma", "chemotherap", "radiotherap", "carcinoid", "melanom",
)

# Tumour-agnostic / basket language: a cross-disease trial the patient MAY still fit.
# Deliberately NARROW. "advanced cancer" / "advanced malignancies" were removed: they
# appear in ordinary SINGLE-tumour trial titles ("Drug X in Advanced Breast Cancer"),
# and marking those as baskets made them cross-tumour-eligible for EVERY patient,
# which silently defeated the disease gate.
_BASKET_MARKERS = (
    "solid tumor", "solid tumors", "solid tumour", "solid tumours",
    "any solid", "any tumor type", "any histology", "basket",
    "tumor-agnostic", "tumour-agnostic", "tumor agnostic", "tumour agnostic",
    "histology-agnostic", "histology agnostic", "histology-independent",
    "histology independent", "regardless of tumor", "regardless of tumour",
    "regardless of histology", "irrespective of tumor", "irrespective of histology",
    "all comers", "pan-tumor", "pan-tumour", "pan tumor", "pan-cancer",
    "multiple tumor types", "multiple tumour types", "multiple cancer types",
    "advanced malignancies of any", "any advanced solid",
)

_PURPOSE_RE = re.compile(r"Primary\s+Purpose:\s*([A-Z_]+)")

# Purposes that count as "treatment studies" (kept when treatment is requested).
# NOTE: "unknown" was REMOVED — an unlabeled study is not evidence of a treatment
# study, it is an absence of evidence, and must not silently clear the purpose gate.
TREATMENT_PURPOSES = {"treatment", "expanded_access"}
# Purposes dropped when the user asked for treatment studies.
NON_TREATMENT_PURPOSES = {
    "diagnostic", "screening", "prevention", "supportive_care", "basic_science",
    "health_services_research", "observational", "device_feasibility", "other",
    "registry", "natural_history", "imaging",
}

# Keyword -> purpose inference, used ONLY when the registry states no Primary Purpose.
# Ordered: the first family whose keyword appears wins.
_PURPOSE_TEXT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("observational", ("registry", "natural history", "observational study", "observational cohort",
                       "prospective cohort", "retrospective cohort", "chart review", "biobank",
                       "specimen collection", "tissue collection", "epidemiolog", "survey study",
                       "biomarker study", "correlative study", "no study drug will be administered")),
    ("diagnostic", ("imaging study", "diagnostic accuracy", "diagnostic performance", "pet/ct",
                    "pet-ct", "pet imaging", "mri imaging", "radiotracer", "tracer uptake",
                    "scintigraphy", "contrast agent", "image quality", "detection rate",
                    "sensitivity and specificity")),
    ("screening", ("screening program", "screening study", "early detection", "cancer screening",
                   "surveillance program")),
    ("prevention", ("prevention of", "prophylaxis", "risk reduction", "chemoprevention")),
    ("supportive_care", ("supportive care", "symptom management", "palliative care",
                         "quality of life intervention")),
)


def disease_families_of(text: str) -> frozenset[str]:
    """Return the cancer family/families named anywhere in `text` (lowercased match)."""
    if not text:
        return frozenset()
    low = text.lower()
    found = {family for family, keys in _DISEASE_FAMILY_KEYWORDS.items()
             if any(k in low for k in keys)}
    # 'small cell lung' is a substring of 'non-small cell lung' — only keep SCLC when it
    # appears standalone (not preceded by 'non-'/'non ') or as the 'sclc' acronym.
    if "small cell lung cancer" in found and not (
        "sclc" in low or re.search(r"(?<!non-)(?<!non )small cell lung", low)
    ):
        found.discard("small cell lung cancer")
    return frozenset(found)


def is_oncology_text(text: str) -> bool:
    """True when the text uses cancer language at all (even without a specific family).

    Used to split the previously-exempt 'no recognized family' bucket into
    'generic oncology study, flag it' and 'not an oncology study, reject it'."""
    low = (text or "").lower()
    return any(m in low for m in _ONCOLOGY_MARKERS)


def is_basket_text(text: str) -> bool:
    """True when the text describes a tumour-agnostic / basket study."""
    low = (text or "").lower()
    return any(m in low for m in _BASKET_MARKERS)


def basket_evidence(text: str) -> str:
    """The basket phrase that fired, for display as evidence (verbatim from `text`)."""
    low = (text or "").lower()
    for m in _BASKET_MARKERS:
        idx = low.find(m)
        if idx != -1:
            return text[idx:idx + len(m)]
    return ""


def primary_purpose(study_type: str, study_design: str) -> str:
    """Classify a trial's primary purpose from the registry's structured fields.

    OBSERVATIONAL type -> observational; otherwise parse the registry 'Primary
    Purpose' field. An INTERVENTIONAL study with NO stated purpose is "unknown",
    NOT "treatment": defaulting to treatment let unlabeled imaging/device studies
    clear the treatment gate. `refine_purpose` then attempts text inference."""
    st = (study_type or "").upper()
    if "OBSERVATIONAL" in st:
        return "observational"
    if "EXPANDED_ACCESS" in st:
        return "expanded_access"
    m = _PURPOSE_RE.search(study_design or "")
    if m:
        return m.group(1).lower()
    return "unknown"


def infer_purpose_from_text(text: str) -> tuple[str, str]:
    """Infer a purpose from title/summary keywords. Returns (purpose, matched keyword)
    or ("", "") when nothing matches. Only consulted when the registry states none."""
    low = (text or "").lower()
    for purpose, keys in _PURPOSE_TEXT_PATTERNS:
        for k in keys:
            if k in low:
                return purpose, k
    return "", ""


def refine_purpose(declared_purpose: str, text: str) -> tuple[str, str]:
    """Resolve the purpose actually used for gating.

    The registry's stated purpose always wins. Only when it is absent/"unknown" do we
    infer from the title + summary, so a genuine treatment trial that merely mentions
    imaging in its summary is never reclassified."""
    declared = (declared_purpose or "").strip().lower()
    if declared and declared != "unknown":
        return declared, ""
    inferred, kw = infer_purpose_from_text(text)
    if inferred:
        return inferred, kw
    return "unknown", ""


def is_treatment_purpose(purpose: str) -> bool:
    return purpose in TREATMENT_PURPOSES


# --- Evidence: verbatim snippets + grounding checks --------------------------------

_WS_RE = re.compile(r"\s+")
_SENT_SPLIT_RE = re.compile(r"(?<=[.;!?])\s+|\n+")


def normalize_for_match(text: str) -> str:
    """Whitespace/case-normalized form used for literal containment checks."""
    return _WS_RE.sub(" ", (text or "")).strip().lower()


def term_in_text(term: str, text: str) -> bool:
    """Substring match, but WORD-BOUNDED for short terms.

    Bare substring matching on short tokens is how "AST" matched "gASTrointestinal" and
    the state code "MI" matched "MIami" / "Memorial" — both produced confident, wrong
    clinical output. Terms of 4 characters or fewer must match as whole words."""
    term = (term or "").strip().lower()
    if not term:
        return False
    low = (text or "").lower()
    if len(term) <= 4:
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", low) is not None
    return term in low


def snippet_for(term: str, text: str, width: int = 160) -> str:
    """A VERBATIM window of `text` around the first occurrence of `term`.

    Returns "" when the term is absent — callers must not invent evidence."""
    if not term or not text:
        return ""
    low, term_low = text.lower(), term.lower()
    idx = low.find(term_low)
    if idx == -1:
        return ""
    half = max((width - len(term)) // 2, 20)
    start = max(idx - half, 0)
    end = min(idx + len(term) + half, len(text))
    out = _WS_RE.sub(" ", text[start:end]).strip()
    if start > 0:
        out = "…" + out
    if end < len(text):
        out = out + "…"
    return out


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT_RE.split(text or "") if s and s.strip()]


def grounded_source(snippet: str, sources: dict[str, str], min_len: int = 8) -> str | None:
    """Return the name of the source field that literally contains `snippet`.

    Whitespace and case are normalized (an LLM re-wrapping a quote is not fabrication),
    but nothing else: paraphrase, invention, or a quote from another trial fails. `None`
    means the claim is UNGROUNDED and must be dropped or flagged, never displayed as
    evidence."""
    needle = normalize_for_match(snippet).strip("…\"' ")
    if len(needle) < min_len:
        return None
    for field, value in sources.items():
        if needle in normalize_for_match(value):
            return field
    return None


# --- Eligibility conflicts: "the patient has something the trial excludes" ---------

_EXCLUSION_CUES = (
    "exclu", "not eligible", "ineligible", "must not have", "may not have",
    "are not permitted", "is not permitted", "contraindicat", "prohibited",
    "no prior", "without a history of", "should not have",
)

# Patient-side term -> the phrases trial text uses for the same thing.
#
# Deliberately SPECIFIC. Bare organ words ("cardiac", "renal") match ordinary prose
# ("excluding cardiac surgery") and would fire a confident, wrong caution, so the
# synonyms are clinical phrases a protocol actually uses in an exclusion list.
_CONDITION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "hepat": ("hepatic impairment", "hepatic insufficiency", "hepatic dysfunction",
              "hepatic disease", "liver function", "liver disease", "hepatitis",
              "transaminase", "bilirubin", "child-pugh", "cirrhosis"),
    "lft": ("hepatic impairment", "hepatic dysfunction", "liver function",
            "transaminase", "bilirubin", "elevated ast", "elevated alt"),
    "liver": ("hepatic impairment", "hepatic dysfunction", "liver function", "liver disease"),
    "renal": ("renal impairment", "renal insufficiency", "renal failure", "renal dysfunction",
              "chronic kidney", "kidney disease", "creatinine clearance", "dialysis"),
    "ckd": ("renal impairment", "renal insufficiency", "renal failure", "chronic kidney",
            "kidney disease", "creatinine clearance", "dialysis"),
    "kidney": ("renal impairment", "renal insufficiency", "chronic kidney", "kidney disease",
               "creatinine clearance"),
    "cardiac": ("heart failure", "cardiac failure", "cardiac dysfunction", "cardiomyopathy",
                "ejection fraction", "lvef", "cardiac disease", "cardiovascular disease",
                "qtc prolongation", "myocardial infarction"),
    "heart": ("heart failure", "cardiac failure", "cardiomyopathy", "ejection fraction",
              "lvef", "cardiac disease", "myocardial infarction"),
    "chf": ("heart failure", "cardiac failure", "ejection fraction", "lvef", "cardiomyopathy"),
    "hypertens": ("uncontrolled hypertension", "hypertension"),
    "diabet": ("diabetes", "diabetic", "hba1c", "uncontrolled glycemic"),
    "autoimmune": ("autoimmune disease", "autoimmune disorder", "immune-mediated",
                   "immune mediated", "autoimmune"),
    "hiv": ("hiv", "human immunodeficiency"),
    "hepatitis b": ("hepatitis b", "hbv"),
    "hepatitis c": ("hepatitis c", "hcv"),
    "brain metasta": ("brain metasta", "cns metasta", "central nervous system metasta",
                      "leptomeningeal"),
    "cns metasta": ("brain metasta", "cns metasta", "leptomeningeal"),
    "pulmonary": ("interstitial lung", "pneumonitis", "pulmonary fibrosis",
                  "obstructive pulmonary", "pulmonary disease"),
    "copd": ("copd", "obstructive pulmonary"),
    "pneumonitis": ("pneumonitis", "interstitial lung"),
    "psychiatric": ("psychiatric disorder", "psychiatric illness", "psychosis"),
    "seizure": ("seizure", "epilep"),
    "thrombo": ("thrombosis", "thromboembolic", "anticoagulation"),
    "neuropath": ("neuropathy", "neuropathic"),
    "infection": ("active infection", "systemic infection", "uncontrolled infection"),
    "pregnan": ("pregnant", "pregnancy", "breastfeeding"),
    "anemia": ("anemia", "haemoglobin", "hemoglobin"),
    "thrombocytopen": ("thrombocytopenia", "platelet count"),
}

# How far after an exclusion cue a condition mention still counts as being excluded.
_EXCLUSION_WINDOW = 200

_ECOG_RANGE_RE = re.compile(r"ecog[^.\n]{0,60}?\b([0-4])\s*(?:-|–|to|or)\s*([0-4])\b", re.I)
_ECOG_MAX_RE = re.compile(r"ecog[^.\n]{0,40}?(?:≤|<=|<|of at most|no (?:greater|higher|worse) than|up to)\s*([0-4])\b", re.I)
_ECOG_EXACT_RE = re.compile(r"ecog(?:\s+(?:ps|performance status))?\s*(?:of|is|=|:)?\s*([0-4])\b(?!\s*(?:-|–|to|or)\s*[0-4])", re.I)


@dataclass(frozen=True)
class EligibilityConflict:
    """A conflict between the patient's record and the trial's own stated limits."""
    text: str            # physician-facing sentence
    snippet: str         # VERBATIM quote from the trial record supporting it
    source_field: str    # which trial field the quote came from
    kind: str            # "ecog" | "exclusion"


def ecog_ceiling(text: str) -> tuple[int, str] | None:
    """Highest ECOG the trial text says it accepts, plus the verbatim sentence.

    Handles "ECOG 0-1", "ECOG performance status ≤ 2", "ECOG performance status of 1"."""
    for sent in sentences(text):
        if "ecog" not in sent.lower():
            continue
        m = _ECOG_RANGE_RE.search(sent)
        if m:
            return max(int(m.group(1)), int(m.group(2))), _WS_RE.sub(" ", sent).strip()
        m = _ECOG_MAX_RE.search(sent)
        if m:
            return int(m.group(1)), _WS_RE.sub(" ", sent).strip()
        m = _ECOG_EXACT_RE.search(sent)
        if m:
            return int(m.group(1)), _WS_RE.sub(" ", sent).strip()
    return None


def _synonyms_for(patient_term: str) -> tuple[str, ...]:
    low = patient_term.lower()
    hits: list[str] = []
    for key, syns in _CONDITION_SYNONYMS.items():
        if key in low:
            hits.extend(syns)
    if not hits:
        # Fall back to the patient's own wording (>=4 chars to avoid noise).
        token = re.sub(r"[^a-z ]", " ", low).strip()
        hits = [token] if len(token) >= 4 else []
    return tuple(dict.fromkeys(hits))


def _excluded_in_sentence(term: str, sentence: str) -> bool:
    """True when `term` appears in `sentence` AFTER an exclusion cue and close to it.

    Position matters: "patients with hepatic impairment are eligible if ... excluded
    prior surgery" mentions both, but only a mention that FOLLOWS the cue (within a
    short window) is plausibly the thing being excluded."""
    low = sentence.lower()
    if not term_in_text(term, low):
        return False
    term_idx = low.find(term.lower())
    for cue in _EXCLUSION_CUES:
        cue_idx = low.find(cue)
        if cue_idx != -1 and 0 <= term_idx - cue_idx <= _EXCLUSION_WINDOW:
            return True
    return False


def eligibility_conflicts(
    trial_text: str,
    *,
    source_field: str,
    patient_conditions: list[str],
    ecog: int | None = None,
) -> list[EligibilityConflict]:
    """Check a patient's conditions/ECOG against the trial's own exclusion language.

    This is the requirements doc's canonical case — "patient has a condition the trial
    excludes" — which the biomarker-only contradiction check never covered. Every hit
    carries the verbatim trial sentence that produced it, so a clinician can judge it."""
    out: list[EligibilityConflict] = []
    if not trial_text or (ecog is None and not patient_conditions):
        return out

    if ecog is not None:
        ceiling = ecog_ceiling(trial_text)
        if ceiling is not None and ecog > ceiling[0]:
            out.append(EligibilityConflict(
                text=(f"Patient ECOG {ecog} exceeds the performance status ceiling "
                      f"({ceiling[0]}) stated in this record — likely exclusion."),
                snippet=ceiling[1], source_field=source_field, kind="ecog",
            ))

    if not patient_conditions:
        return out
    excl_sents = [s for s in sentences(trial_text)
                  if any(cue in s.lower() for cue in _EXCLUSION_CUES)]
    if not excl_sents:
        return out

    seen: set[str] = set()
    for cond in patient_conditions:
        if not cond or not cond.strip():
            continue
        for syn in _synonyms_for(cond):
            for sent in excl_sents:
                if _excluded_in_sentence(syn, sent):
                    key = f"{cond}|{sent[:60]}"
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(EligibilityConflict(
                        text=(f"Patient record notes '{cond}'; this trial's text lists a "
                              f"matching exclusion — verify eligibility."),
                        snippet=_WS_RE.sub(" ", sent).strip()[:240],
                        source_field=source_field, kind="exclusion",
                    ))
                    break
            else:
                continue
            break
    return out
