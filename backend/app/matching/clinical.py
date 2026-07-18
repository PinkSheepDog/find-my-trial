"""Clinical normalization used as GATES before biomarker/semantic overlap is rewarded.

The prior ranking let a wrong-primary-cancer or an imaging/registry study outrank the
right treatment trial because biomarker/lexical overlap dominated. This module gives
retrieval two hard signals it was missing:

  * disease_families_of(text) -> the cancer family/families named in a trial's
    conditions or a patient's diagnosis (breast, lung, pancreatic, ...). Retrieval
    rejects trials whose family is disjoint from the patient's, unless the trial is a
    tumour-agnostic / biomarker basket.
  * primary_purpose(study_type, study_design) -> treatment | diagnostic | screening |
    prevention | supportive_care | observational | ... parsed from the registry's
    "Primary Purpose" field. When treatment studies are requested, non-treatment
    purposes are dropped.

Deterministic and offline; no model calls.
"""

from __future__ import annotations

import re

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
}

# Tumour-agnostic / basket language: a cross-disease trial the patient MAY still fit.
_BASKET_MARKERS = (
    "solid tumor", "solid tumors", "solid tumour", "solid tumours", "advanced solid",
    "any solid", "basket", "tumor-agnostic", "tumour-agnostic", "tumor agnostic",
    "histology-agnostic", "histology agnostic", "regardless of tumor", "regardless of histology",
    "advanced cancer", "advanced malignancies", "advanced malignancy", "all comers", "pan-tumor",
)

_PURPOSE_RE = re.compile(r"Primary\s+Purpose:\s*([A-Z_]+)")

# Purposes that count as "treatment studies" (kept when treatment is requested).
TREATMENT_PURPOSES = {"treatment", "expanded_access", "unknown"}
# Purposes dropped when the user asked for treatment studies.
NON_TREATMENT_PURPOSES = {
    "diagnostic", "screening", "prevention", "supportive_care", "basic_science",
    "health_services_research", "observational", "device_feasibility", "other",
}


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


def is_basket_text(text: str) -> bool:
    """True when the text describes a tumour-agnostic / basket study."""
    low = (text or "").lower()
    return any(m in low for m in _BASKET_MARKERS)


def primary_purpose(study_type: str, study_design: str) -> str:
    """Classify a trial's primary purpose. OBSERVATIONAL type -> observational;
    otherwise parse the registry 'Primary Purpose' field; INTERVENTIONAL without an
    explicit purpose defaults to treatment."""
    st = (study_type or "").upper()
    if "OBSERVATIONAL" in st:
        return "observational"
    if "EXPANDED_ACCESS" in st:
        return "expanded_access"
    m = _PURPOSE_RE.search(study_design or "")
    if m:
        return m.group(1).lower()
    if "INTERVENTIONAL" in st:
        return "treatment"
    return "unknown"
