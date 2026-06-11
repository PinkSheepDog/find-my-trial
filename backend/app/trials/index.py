"""Load the trial CSV into normalized in-memory records + a BM25 index.

The index is built once at startup. A content hash of the CSV guards any on-disk
cache so a changed corpus can never be served from a stale index (a defect in the
prior prototype, whose BERT cache was keyed only on model name).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pandas as pd
from rank_bm25 import BM25Okapi

ACTIVE_STATUSES = {
    "RECRUITING", "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING",
    "ACTIVE_NOT_RECRUITING", "AVAILABLE",
}
RECRUITING_STATUSES = {"RECRUITING", "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING", "AVAILABLE"}

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "for", "with", "on", "study",
    "trial", "patients", "patient", "clinical", "treatment", "phase",
}


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


@dataclass
class TrialRecord:
    nct: str
    title: str
    url: str
    status: str
    phase: str
    study_type: str
    sponsor: str
    brief_summary: str
    conditions: list[str]
    interventions: list[str]
    locations: list[str]
    sex: str            # "FEMALE" | "MALE" | "ALL" | "NA"
    age_buckets: set[str]
    # derived
    condition_text: str = ""
    intervention_text: str = ""
    is_interventional: bool = False
    search_text: str = ""
    tokens: list[str] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_recruiting(self) -> bool:
        return self.status in RECRUITING_STATUSES


def _split_multi(value) -> list[str]:
    if value is None or (isinstance(value, float)):
        return []
    parts = re.split(r"[|;\n]", str(value))
    return [p.strip() for p in parts if p and p.strip()]


def _age_buckets(age_value) -> set[str]:
    if not age_value:
        return set()
    return {p.strip().upper() for p in str(age_value).split(",") if p.strip()}


class TrialIndex:
    def __init__(self, records: list[TrialRecord], content_hash: str) -> None:
        self.records = records
        self.content_hash = content_hash
        self._bm25 = BM25Okapi([r.tokens for r in records])
        self._by_nct = {r.nct: i for i, r in enumerate(records)}

    @classmethod
    def from_csv(cls, csv_path: str | Path) -> "TrialIndex":
        csv_path = Path(csv_path)
        content_hash = _hash_file(csv_path)
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
        records: list[TrialRecord] = []
        for row in df.to_dict("records"):
            conditions = _split_multi(row.get("Conditions", ""))
            interventions = [_clean_intervention(i) for i in _split_multi(row.get("Interventions", ""))]
            interventions = [i for i in interventions if i]
            locations = _split_multi(row.get("Locations", ""))
            title = (row.get("Study Title") or "").strip()
            brief = (row.get("Brief Summary") or "").strip()
            study_type = (row.get("Study Type") or "").strip().upper()
            status = ((row.get("Study Status") or "").strip().upper()) or "UNKNOWN"
            phase = (row.get("Phases") or "").strip()
            sponsor = (row.get("Sponsor") or "").strip()
            sex = ((row.get("Sex") or "").strip().upper()) or "ALL"

            condition_text = " ".join(conditions)
            intervention_text = " ".join(interventions)
            search_text = " ".join([title, brief, condition_text, intervention_text, phase])

            rec = TrialRecord(
                nct=(row.get("NCT Number") or "").strip(),
                title=title, url=(row.get("Study URL") or "").strip(),
                status=status, phase=phase, study_type=study_type, sponsor=sponsor,
                brief_summary=brief, conditions=conditions, interventions=interventions,
                locations=locations, sex=sex, age_buckets=_age_buckets(row.get("Age", "")),
                condition_text=condition_text, intervention_text=intervention_text,
                is_interventional="INTERVENTIONAL" in study_type,
                search_text=search_text, tokens=_tokenize(search_text),
            )
            if rec.nct:
                records.append(rec)
        return cls(records, content_hash)

    def bm25_scores(self, query_tokens: list[str]):
        return self._bm25.get_scores(query_tokens)

    def get(self, nct: str) -> TrialRecord | None:
        idx = self._by_nct.get(nct)
        return self.records[idx] if idx is not None else None

    def stats(self) -> dict:
        return {
            "trial_count": len(self.records),
            "active_count": sum(r.is_active for r in self.records),
            "interventional_count": sum(r.is_interventional for r in self.records),
            "content_hash": self.content_hash[:12],
        }


def _clean_intervention(value: str) -> str:
    v = value.strip()
    if ":" in v:
        prefix, rest = v.split(":", 1)
        if prefix.isupper():  # "DRUG: Tucatinib" -> "Tucatinib"
            v = rest.strip()
    return v


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@lru_cache(maxsize=1)
def get_index(csv_path: str) -> TrialIndex:
    return TrialIndex.from_csv(csv_path)
