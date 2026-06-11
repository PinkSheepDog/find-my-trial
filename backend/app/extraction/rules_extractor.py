"""Deterministic, negation-aware patient extractor.

This is the always-available fallback (no API key, no network) and the reference
implementation that pins down the schema's semantics. Its defining feature — the
thing the old prototype got catastrophically wrong — is BIOMARKER DIRECTION:
every biomarker mention is classified positive / negative / low / equivocal by
inspecting the local context window for negation and level cues.

It runs on DE-IDENTIFIED text (tags like [NAME]/[DATE] are inert here).
"""

from __future__ import annotations

import re

from app.extraction.schema import (
    Biomarker,
    BiomarkerStatus,
    Certainty,
    Evidence,
    PatientProfile,
    Therapy,
)

# --- Biomarkers we recognize, with the regex that finds a mention ---
_BIOMARKERS = {
    "HER2": r"\bher2\b|\berbb2\b",
    "ER": r"\b(?:estrogen receptor|er)\b",
    "PR": r"\b(?:progesterone receptor|pr)\b",
    "EGFR": r"\begfr\b",
    "ALK": r"\balk\b",
    "ROS1": r"\bros1\b",
    "BRAF": r"\bbraf\b",
    "KRAS": r"\bkras\b",
    "BRCA": r"\bbrca(?:1|2)?\b",
    "PD-L1": r"\bpd[\s\-]?l1\b",
    "MSI": r"\bmsi\b|\bmicrosatellite instabilit",
    "HRD": r"\bhrd\b|\bhomologous recombination def",
}

# Negation cues in the local window  -> NEGATIVE
_NEG_CUES = (
    "negative", "neg", "not amplified", "non-amplified", "wild type", "wildtype",
    "wild-type", "absent", "no evidence", "not detected", "not mutated", "stable",
    "intact", "proficient", "mss",  # MSI-stable / MS-stable
)
# Low cues -> LOW (clinically distinct from positive!)
_LOW_CUES = ("low", "ihc 1+", "ihc 0", "1+", "equivocal low", "borderline low")
# Positive cues -> POSITIVE
_POS_CUES = (
    "positive", "pos", "amplified", "mutated", "mutation", "overexpress",
    "high", "elevated", "detected", "present", "ihc 3+",
)
# Equivocal cues
_EQUIV_CUES = ("equivocal", "borderline", "indeterminate", "pending", "ihc 2+")

_THERAPIES = {
    "Trastuzumab": r"\btrastuzumab\b|\bherceptin\b",
    "Pertuzumab": r"\bpertuzumab\b",
    "Paclitaxel": r"\bpaclitaxel\b",
    "Nab-paclitaxel": r"\bnab[\s\-]?paclitaxel\b",
    "Carboplatin": r"\bcarboplatin\b",
    "Cisplatin": r"\bcisplatin\b",
    "Gemcitabine": r"\bgemcitabine\b",
    "Doxorubicin": r"\bdoxorubicin\b|\badriamycin\b",
    "Cyclophosphamide": r"\bcyclophosphamide\b",
    "ddAC": r"\bdd[\s\-]?ac\b",
    "Capecitabine": r"\bcapecitabine\b|\bxeloda\b",
    "Atezolizumab": r"\batezolizumab\b|\btecentriq\b",
    "Pembrolizumab": r"\bpembrolizumab\b|\bkeytruda\b",
    "Nivolumab": r"\bnivolumab\b",
    "Osimertinib": r"\bosimertinib\b",
    "Tucatinib": r"\btucatinib\b",
    "Trastuzumab deruxtecan": r"\btrastuzumab deruxtecan\b|\bt-?dxd\b|\benhertu\b",
}

_CANCER_TYPES = {
    "Triple-Negative Breast Cancer": r"\btriple[\s\-]?negative\b|\btnbc\b",
    "Breast Cancer": r"\bbreast cancer\b|\binvasive ductal carcinoma\b|\bidc\b",
    "Non-Small Cell Lung Cancer": r"\bnsclc\b|\bnon[\s\-]?small cell lung\b",
    "Small Cell Lung Cancer": r"\bsclc\b|\bsmall cell lung\b",
    "Lung Cancer": r"\blung cancer\b",
    "Colorectal Cancer": r"\bcolorectal\b|\bcolon cancer\b|\brectal cancer\b",
    "Ovarian Cancer": r"\bovarian cancer\b",
    "Prostate Cancer": r"\bprostate cancer\b",
    "Pancreatic Cancer": r"\bpancreatic cancer\b",
    "Melanoma": r"\bmelanoma\b",
    "Lymphoma": r"\blymphoma\b",
    "Leukemia": r"\bleukemia\b",
    "Glioblastoma": r"\bglioblastoma\b|\bgbm\b",
}

_SITE_PATTERNS = {
    "Liver": r"\b(?:liver|hepatic)\b",
    "Bone": r"\b(?:bone|osseous|spine|pelvis)\b",
    "Lung": r"\b(?:lung nodul|pulmonary|pleural)\b",
    "Brain": r"\bbrain\b|\bcerebral\b",
}

_COMORBIDITIES = {
    "Hypertension": r"\bhypertension\b|\bhtn\b",
    "Type 2 Diabetes": r"\btype 2 diabetes\b|\bdm2\b|\bt2dm\b|\bdiabetes mellitus type 2\b",
    "Chronic Kidney Disease": r"\bckd\b|\bchronic kidney disease\b",
    "Hyperlipidemia": r"\bhyperlipidemia\b|\bhigh cholesterol\b",
    "Anemia": r"\banemia\b|\banaemia\b",
    "Anxiety": r"\banxiety\b",
}


class RulesExtractor:
    name = "rules"

    def extract(self, text: str) -> PatientProfile:
        lower = text.lower()
        evidence: list[Evidence] = []
        uncertain: list[str] = []

        age = self._age(text)
        sex = self._sex(text)
        cancer_types = self._match_catalog(lower, _CANCER_TYPES)
        stage, metastatic = self._stage(lower)
        sites = self._match_catalog(lower, _SITE_PATTERNS)
        biomarkers = self._biomarkers(text, lower, evidence, uncertain)
        therapies = self._therapies(text, lower)
        ecog = self._ecog(lower)
        comorbidities = self._match_catalog(lower, _COMORBIDITIES)
        organ_flags = self._organ_flags(lower)
        locations = self._locations(text)

        diagnosis = self._diagnosis(cancer_types, stage, metastatic)

        for f, present in [
            ("age", age is not None), ("sex", bool(sex)),
            ("ecog", ecog is not None), ("stage", bool(stage)),
        ]:
            if not present:
                uncertain.append(f)

        return PatientProfile(
            age=age, sex=sex, diagnosis=diagnosis, cancer_types=cancer_types,
            stage=stage, is_metastatic=metastatic, disease_sites=sites,
            biomarkers=biomarkers, therapies=therapies, ecog=ecog,
            comorbidities=comorbidities, organ_function_flags=organ_flags,
            location_preferences=locations, evidence=evidence,
            missing_or_uncertain=sorted(set(uncertain)), extractor=self.name,
        )

    # ----------------------------- biomarkers (the core) -----------------------------
    def _biomarkers(self, text, lower, evidence, uncertain) -> list[Biomarker]:
        found: dict[str, Biomarker] = {}
        for name, pat in _BIOMARKERS.items():
            for m in re.finditer(pat, lower):
                window = self._window(lower, m.start(), m.end(), radius=40)
                status, detail = self._classify_status(name, window)
                snippet = self._window(text, m.start(), m.end(), radius=35).strip()
                existing = found.get(name)
                # Prefer a more decisive (non-unknown) classification if we see the
                # marker multiple times; flag genuine conflicts as uncertain.
                if existing is None:
                    found[name] = Biomarker(name=name, status=status, detail=detail)
                    evidence.append(Evidence(field=f"biomarker:{name}", snippet=snippet))
                elif existing.status != status and BiomarkerStatus.UNKNOWN not in {existing.status, status}:
                    uncertain.append(f"biomarker:{name} (conflicting mentions)")
                    found[name].certainty = Certainty.UNCERTAIN
                elif existing.status == BiomarkerStatus.UNKNOWN and status != BiomarkerStatus.UNKNOWN:
                    found[name] = Biomarker(name=name, status=status, detail=detail)

        # TNBC implies ER-/PR-/HER2- if not otherwise stated (inferred).
        if any("Triple-Negative" in c for c in self._match_catalog(lower, _CANCER_TYPES)):
            for marker in ("ER", "PR", "HER2"):
                if marker not in found:
                    found[marker] = Biomarker(
                        name=marker, status=BiomarkerStatus.NEGATIVE,
                        detail="inferred from TNBC", certainty=Certainty.INFERRED,
                    )
        return list(found.values())

    def _classify_status(self, name, window) -> tuple[BiomarkerStatus, str | None]:
        """Classify a biomarker mention by inspecting its local context window.
        Order matters: LOW and NEGATIVE cues win over POSITIVE, because a HER2-low or
        BRCA-negative finding must never be promoted to positive."""
        detail = None

        # Special-case HER2 levels: IHC 1+ / FISH not amplified => LOW; IHC 3+ => POSITIVE.
        if name == "HER2":
            if re.search(r"ihc\s*1\+|ihc\s*0|her2[\s\-]?low|fish[^.]*not amplified|not amplified", window):
                detail = self._grab(window, r"ihc\s*[0-3]\+|fish[^.,;]*|not amplified|her2[\s\-]?low")
                return BiomarkerStatus.LOW, detail
            if re.search(r"ihc\s*3\+|amplified(?!\s*not)|her2[\s\-]?positive", window):
                return BiomarkerStatus.POSITIVE, self._grab(window, r"ihc\s*3\+|amplified|positive")
            if re.search(r"ihc\s*2\+", window):
                return BiomarkerStatus.EQUIVOCAL, "IHC 2+"

        # MSI: "stable"/"MSS" is negative-for-instability; "high"/"MSI-H" is positive.
        if name == "MSI":
            if re.search(r"\bstable\b|\bmss\b", window):
                return BiomarkerStatus.NEGATIVE, "stable"
            if re.search(r"\bhigh\b|\bmsi[\s\-]?h\b|instability high", window):
                return BiomarkerStatus.POSITIVE, "high"

        # General cue scan — NEGATIVE and LOW take precedence over POSITIVE.
        if any(cue in window for cue in _NEG_CUES):
            return BiomarkerStatus.NEGATIVE, self._grab(window, "|".join(map(re.escape, _NEG_CUES)))
        if any(cue in window for cue in _LOW_CUES):
            return BiomarkerStatus.LOW, self._grab(window, "|".join(map(re.escape, _LOW_CUES)))
        if any(cue in window for cue in _EQUIV_CUES):
            return BiomarkerStatus.EQUIVOCAL, None
        if any(cue in window for cue in _POS_CUES):
            return BiomarkerStatus.POSITIVE, self._grab(window, "|".join(map(re.escape, _POS_CUES)))
        return BiomarkerStatus.UNKNOWN, detail

    @staticmethod
    def _grab(window, pat) -> str | None:
        m = re.search(pat, window)
        return m.group(0).strip() if m else None

    @staticmethod
    def _window(text, start, end, radius=40) -> str:
        """Local context window, clamped to the nearest clause/sentence boundaries so
        a cue from an adjacent clause (e.g. 'BRCA negative. PD-L1 positive') cannot
        leak across the period/semicolon into this marker's classification."""
        left = max(0, start - radius)
        right = min(len(text), end + radius)
        # Pull the left edge forward to just after the last clause break before the marker.
        left_chunk = text[left:start]
        for sep in (". ", "; ", "\n", ", "):
            idx = left_chunk.rfind(sep)
            if idx != -1:
                left = max(left, left + idx + len(sep))
        # Push the right edge back to the first clause break after the marker.
        right_chunk = text[end:right]
        for sep in (". ", "; ", "\n"):
            idx = right_chunk.find(sep)
            if idx != -1:
                right = min(right, end + idx)
        return text[left:right]

    # ----------------------------- other fields -----------------------------
    def _therapies(self, text, lower) -> list[Therapy]:
        out: list[Therapy] = []
        for name, pat in _THERAPIES.items():
            m = re.search(pat, lower)
            if not m:
                continue
            # Toxicity often appears a clause or two after the drug ("atezolizumab
            # ... tolerated initially then hepatitis immune-mediated grade 3"), so use
            # a wider, NON clause-bounded forward window for adverse-event linkage.
            tox_window = lower[max(0, m.start() - 30): min(len(lower), m.end() + 160)]
            tox = None
            if re.search(r"hepatitis|immune[\s\-]mediated|grade\s*[34]|colitis|pneumonitis|toxicity", tox_window):
                tox = self._grab(
                    tox_window,
                    r"grade\s*[34][^.,;]*|immune[\s\-]mediated[^.,;]*hepatitis|hepatitis|colitis|pneumonitis",
                )
            out.append(Therapy(name=name, caused_toxicity=tox))
        return out

    def _match_catalog(self, lower, catalog) -> list[str]:
        return [label for label, pat in catalog.items() if re.search(pat, lower)]

    def _age(self, text) -> int | None:
        m = re.search(r"\b(\d{1,3})\s*(?:year[s]?[\s\-]old|y/?o|yo|yrs?[\s\-]old)\b", text, re.I)
        if m:
            return int(m.group(1))
        m = re.search(r"\bage\s*[:#]?\s*(\d{1,3})\b", text, re.I)
        if m:
            return int(m.group(1))
        m = re.search(r"\b(\d{2,3})\s*[FM]\b", text)  # "64F"
        if m:
            return int(m.group(1))
        m = re.search(r"\b[FM]\s+(\d{2,3})\b", text)  # "F 64"
        if m and 18 <= int(m.group(1)) <= 120:
            return int(m.group(1))
        return None

    def _sex(self, text) -> str | None:
        lower = text.lower()
        if re.search(r"\bfemale\b|\bwoman\b|\b\d{2,3}\s*f\b", lower):
            return "Female"
        if re.search(r"\bmale\b|\bman\b|\b\d{2,3}\s*m\b", lower):
            return "Male"
        # Standalone demographic token: "F 64" / "64 F" (case-sensitive to avoid
        # matching the letter f/m inside words).
        if re.search(r"\bF\s+\d{2,3}\b|\b\d{2,3}\s+F\b", text):
            return "Female"
        if re.search(r"\bM\s+\d{2,3}\b|\b\d{2,3}\s+M\b", text):
            return "Male"
        return None

    def _stage(self, lower) -> tuple[str | None, bool]:
        metastatic = bool(re.search(r"\bmetastatic\b|\bmetastas[ie]s\b|\bstage iv\b|\bmet\b", lower))
        m = re.search(r"\bstage\s+(iv|iii|ii|i|0|[1-4])([abc])?\b", lower)
        stage = None
        if m:
            stage = "Stage " + m.group(1).upper() + (m.group(2).upper() if m.group(2) else "")
        elif metastatic:
            stage = "Metastatic"
        return stage, metastatic

    def _ecog(self, lower) -> int | None:
        m = re.search(r"\becog\s*[:#]?\s*([0-4])\b", lower)
        return int(m.group(1)) if m else None

    def _organ_flags(self, lower) -> list[str]:
        flags = []
        if re.search(r"\bckd\s*(?:stage\s*)?(?:ii|2)\b", lower):
            flags.append("CKD stage II")
        if re.search(r"lft\s*(?:elevation|↑|elevated)|transaminitis|ast\s*\d|alt\s*\d", lower):
            flags.append("LFT elevation")
        if re.search(r"\banemia\b|\banaemia\b|hb\s*(?:10|9|8)", lower):
            flags.append("anemia")
        return flags

    def _locations(self, text) -> list[str]:
        out: list[str] = []
        # explicit preference statements
        for m in re.finditer(r"prefers?\s+([A-Za-z/,\s]+?)(?:\.|\n|$|open to|if)", text, re.I):
            chunk = m.group(1)
            out += [c.strip() for c in re.split(r"[/,]", chunk) if 2 < len(c.strip()) < 30]
        for m in re.finditer(r"\b(?:lives in|located in|from)\s+([A-Z][A-Za-z]+)", text):
            out.append(m.group(1))
        # dedupe, drop redaction tags
        seen, res = set(), []
        for c in out:
            cl = c.strip().strip(".")
            if cl and "[" not in cl and cl.lower() not in seen:
                seen.add(cl.lower())
                res.append(cl)
        return res[:6]

    def _diagnosis(self, cancer_types, stage, metastatic) -> str | None:
        if not cancer_types:
            return None
        primary = cancer_types[0]
        prefix = "Metastatic " if metastatic else (f"{stage} " if stage else "")
        return f"{prefix}{primary}".strip()
