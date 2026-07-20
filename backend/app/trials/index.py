"""Load the trial CSV into normalized in-memory records + a BM25 index.

The index is built once at startup and then cached on disk. The cache is keyed by
(cache_format_version, content_hash, NORMALIZATION_VERSION): a changed corpus or a
changed normalization rule can never be served from a stale index (a defect in the
prior prototype, whose BERT cache was keyed only on model name). Any cache whose key
does not match — or that is truncated, corrupt or structurally wrong — is REFUSED
with a logged reason and rebuilt from the CSV.

Loading is also defensive about the corpus itself. A CSV that is missing a required
column, that is mostly empty, or that is heavily duplicated fails LOUDLY rather than
producing a silently degraded index:

  * required columns are asserted up front (CorpusSchemaError names the missing ones)
  * rows with no NCT id are dropped and counted
  * duplicate NCT ids are collapsed and counted
  * unparseable `Last Update Posted` values are skipped and counted
  * a floor on kept rows / ceiling on dropped rows raises CorpusQualityError

Every one of those counters is surfaced in manifest() so a degraded corpus is visible
in /health instead of silently shipping.

Cache trust boundary: the payload is pickled, so the cache directory is trusted input
(it sits next to the corpus and is written only by this process). The key is stored in
a plaintext JSON header that is read and verified BEFORE the pickle payload is touched,
so a stale or mismatched cache is rejected without unpickling it.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import pickle
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from rank_bm25 import BM25Okapi

from app.matching.clinical import disease_families_of, is_basket_text, primary_purpose

log = logging.getLogger(__name__)

# Bump when tokenization, disease-family/purpose logic, or record derivation changes.
# The on-disk cache is keyed by (content_hash, NORMALIZATION_VERSION, cache format) so a
# logic change can never be served from a stale index.
# Bump whenever a DERIVED field changes (disease_families, is_basket, study_purpose,
# tokens). The on-disk index cache is keyed by this value, so failing to bump it makes
# a machine with an older cache serve stale gate data — the gates would silently run
# on the previous classification. 1.2.0: expanded disease families 23 -> 39, narrowed
# basket markers, purpose no longer defaults to "treatment" when the registry is silent.
NORMALIZATION_VERSION = "1.2.0-disease-purpose-gates"

# Bump when the cached payload's SHAPE changes (new/renamed TrialRecord fields, new
# payload keys). Old caches are then refused rather than unpickled into a wrong shape.
CACHE_FORMAT_VERSION = 2

# Columns the loader actually reads. A corpus missing any of these would otherwise load
# with every affected field silently blank.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "NCT Number", "Study Title", "Study URL", "Study Status", "Brief Summary",
    "Conditions", "Interventions", "Locations", "Sponsor", "Sex", "Age",
    "Phases", "Study Type", "Study Design", "Last Update Posted",
)

# Sanity floors so a badly degraded corpus cannot silently reach production.
DEFAULT_MIN_ROWS = 100
DEFAULT_MAX_DROP_RATIO = 0.10

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

# Accepted `Last Update Posted` shapes. ClinicalTrials.gov exports ISO-8601, but
# hand-edited / re-exported corpora routinely carry US-style or partial dates.
_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%d %B %Y")


class CorpusError(RuntimeError):
    """Base class for a corpus that cannot be turned into a trustworthy index."""


class CorpusSchemaError(CorpusError):
    """The CSV is missing columns the loader depends on."""


class CorpusQualityError(CorpusError):
    """The CSV parsed, but too few rows survived to be trusted."""


class EmptyCorpusError(CorpusError):
    """No usable trial records at all."""


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


def _parse_date(value: str) -> datetime.date | None:
    """Parse a corpus date, or None if it is not a date we recognise.

    Never falls back to lexicographic comparison: a M/D/YYYY value sorts wrongly as a
    string and would silently corrupt `data_current_through`.
    """
    v = (value or "").strip()
    if not v:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class LoadStats:
    """Row-level accounting for one corpus load. Surfaced verbatim in the manifest."""
    raw_row_count: int = 0
    rows_missing_nct: int = 0
    duplicate_rows_dropped: int = 0
    unparseable_dates: int = 0
    missing_dates: int = 0
    schema_fingerprint: str = ""
    schema_column_count: int = 0

    @property
    def rows_dropped_total(self) -> int:
        return self.rows_missing_nct + self.duplicate_rows_dropped

    @classmethod
    def from_dict(cls, data: dict) -> "LoadStats":
        """Rebuild from as_dict(), ignoring derived keys like rows_dropped_total."""
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in fields})

    def as_dict(self) -> dict:
        return {
            "raw_row_count": self.raw_row_count,
            "rows_missing_nct": self.rows_missing_nct,
            "duplicate_rows_dropped": self.duplicate_rows_dropped,
            "rows_dropped_total": self.rows_dropped_total,
            "unparseable_dates": self.unparseable_dates,
            "missing_dates": self.missing_dates,
            "schema_fingerprint": self.schema_fingerprint,
            "schema_column_count": self.schema_column_count,
        }


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
    # clinical gates
    disease_families: frozenset[str] = field(default_factory=frozenset)
    study_purpose: str = "unknown"   # treatment | diagnostic | screening | observational | ...
    is_basket: bool = False          # tumour-agnostic / solid-tumor basket study

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


def _schema_fingerprint(columns) -> str:
    """Fingerprint the ordered column set so a column add/remove/rename is detectable."""
    joined = "\x1f".join(str(c) for c in columns)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


class TrialIndex:
    def __init__(self, records: list[TrialRecord], content_hash: str,
                 data_current_through: str = "", load_stats: LoadStats | None = None,
                 loaded_from_cache: bool = False) -> None:
        if not records:
            raise EmptyCorpusError(
                "Refusing to build a trial index from zero records: the corpus is empty or "
                "its schema is invalid (no row carried a usable NCT id). BM25 cannot be built "
                "over an empty document set."
            )
        self.records = records
        self.content_hash = content_hash
        self.data_current_through = data_current_through
        self.load_stats = load_stats or LoadStats(raw_row_count=len(records))
        self.loaded_from_cache = loaded_from_cache
        self.normalization_version = NORMALIZATION_VERSION
        self.built_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
        self._bm25 = BM25Okapi([r.tokens for r in records])
        self._by_nct = {r.nct: i for i, r in enumerate(records)}

    def manifest(self) -> dict:
        """Build provenance — enough to detect a stale, degraded or incompatible index.

        `content_hash` fingerprints the corpus bytes; `schema_fingerprint` fingerprints
        the column set (a column change moves the file hash too, but the schema print
        says *what* changed). The drop counters make a degraded corpus visible rather
        than letting it ship silently.
        """
        return {
            "row_count": len(self.records),
            "content_hash": self.content_hash[:32],
            "normalization_version": self.normalization_version,
            "data_current_through": self.data_current_through,
            "built_at": self.built_at,
            "loaded_from_cache": self.loaded_from_cache,
            **self.load_stats.as_dict(),
        }

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_csv(cls, csv_path: str | Path, *, use_cache: bool = True,
                 cache_dir: str | Path | None = None,
                 min_rows: int = DEFAULT_MIN_ROWS,
                 max_drop_ratio: float = DEFAULT_MAX_DROP_RATIO) -> "TrialIndex":
        """Build an index from the corpus CSV, via the on-disk cache when it is valid.

        Raises CorpusSchemaError if a required column is absent, CorpusQualityError if
        too little of the corpus survived parsing, EmptyCorpusError if nothing did.
        """
        csv_path = Path(csv_path)
        content_hash = _hash_file(csv_path)
        cache_path = _cache_path(csv_path, cache_dir)

        if use_cache:
            cached = _read_cache(cache_path, content_hash)
            if cached is not None:
                return cls(cached["records"], content_hash,
                           data_current_through=cached["data_current_through"],
                           load_stats=LoadStats.from_dict(cached["load_stats"]),
                           loaded_from_cache=True)

        records, latest, stats = _parse_csv(csv_path, min_rows=min_rows,
                                            max_drop_ratio=max_drop_ratio)
        index = cls(records, content_hash,
                    data_current_through=latest.isoformat() if latest else "",
                    load_stats=stats)
        if use_cache:
            _write_cache(cache_path, content_hash, index)
        return index

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


def _parse_csv(csv_path: Path, *, min_rows: int, max_drop_ratio: float
               ) -> tuple[list[TrialRecord], datetime.date | None, LoadStats]:
    """Parse + normalize the CSV, deduplicating by NCT and accounting for every row.

    Dedup strategy: one record per NCT, keeping the row with the LATEST parseable
    `Last Update Posted`. Ties, unparseable and missing dates keep the FIRST occurrence,
    so the result is deterministic regardless of row order. Output order is first-seen
    order.
    """
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise CorpusSchemaError(
            f"Trial corpus {csv_path} is missing {len(missing)} required column(s): "
            f"{', '.join(missing)}. Present columns: {', '.join(map(str, df.columns))}. "
            "Refusing to build an index — these fields would silently load as blank."
        )

    stats = LoadStats(
        raw_row_count=len(df),
        schema_fingerprint=_schema_fingerprint(df.columns),
        schema_column_count=len(df.columns),
    )

    records: list[TrialRecord] = []
    # nct -> (position in `records`, parsed last-update date or None)
    seen: dict[str, tuple[int, datetime.date | None]] = {}

    for row in df.to_dict("records"):
        nct = (row.get("NCT Number") or "").strip()
        if not nct:
            stats.rows_missing_nct += 1
            continue

        raw_upd = (row.get("Last Update Posted") or "").strip()
        if not raw_upd:
            stats.missing_dates += 1
            upd = None
        else:
            upd = _parse_date(raw_upd)
            if upd is None:
                stats.unparseable_dates += 1

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
        study_design = (row.get("Study Design") or "").strip()

        # Disease family is read from CONDITIONS + TITLE only (not the free-text
        # summary), so a trial that merely mentions another cancer in prose is not
        # mis-classified into that family.
        family_text = " ".join([condition_text, title])

        rec = TrialRecord(
            nct=nct,
            title=title, url=(row.get("Study URL") or "").strip(),
            status=status, phase=phase, study_type=study_type, sponsor=sponsor,
            brief_summary=brief, conditions=conditions, interventions=interventions,
            locations=locations, sex=sex, age_buckets=_age_buckets(row.get("Age", "")),
            condition_text=condition_text, intervention_text=intervention_text,
            is_interventional="INTERVENTIONAL" in study_type,
            search_text=search_text, tokens=_tokenize(search_text),
            disease_families=disease_families_of(family_text),
            study_purpose=primary_purpose(study_type, study_design),
            is_basket=is_basket_text(family_text),
        )

        prev = seen.get(nct)
        if prev is None:
            seen[nct] = (len(records), upd)
            records.append(rec)
            continue

        # Duplicate NCT: keep whichever row is more recently updated.
        stats.duplicate_rows_dropped += 1
        prev_pos, prev_date = prev
        if upd is not None and (prev_date is None or upd > prev_date):
            records[prev_pos] = rec
            seen[nct] = (prev_pos, upd)

    _assert_corpus_quality(csv_path, records, stats, min_rows=min_rows,
                           max_drop_ratio=max_drop_ratio)

    latest = max((d for _, d in seen.values() if d is not None), default=None)
    if stats.rows_dropped_total or stats.unparseable_dates:
        log.warning(
            "corpus %s: kept %d/%d rows (dropped %d no-NCT, %d duplicate NCT); "
            "%d unparseable and %d missing update dates",
            csv_path.name, len(records), stats.raw_row_count, stats.rows_missing_nct,
            stats.duplicate_rows_dropped, stats.unparseable_dates, stats.missing_dates,
        )
    return records, latest, stats


def _assert_corpus_quality(csv_path: Path, records: list[TrialRecord], stats: LoadStats,
                           *, min_rows: int, max_drop_ratio: float) -> None:
    """Fail loudly on a corpus too degraded to serve."""
    if not records:
        raise EmptyCorpusError(
            f"Trial corpus {csv_path} yielded 0 usable records out of {stats.raw_row_count} "
            f"row(s): the corpus is empty or its schema is invalid "
            f"({stats.rows_missing_nct} row(s) had no NCT id)."
        )
    if len(records) < min_rows:
        raise CorpusQualityError(
            f"Trial corpus {csv_path} yielded only {len(records)} usable record(s), below the "
            f"floor of {min_rows}. Refusing to serve a degraded corpus "
            f"(raw rows={stats.raw_row_count}, dropped={stats.rows_dropped_total})."
        )
    if stats.raw_row_count:
        drop_ratio = stats.rows_dropped_total / stats.raw_row_count
        if drop_ratio > max_drop_ratio:
            raise CorpusQualityError(
                f"Trial corpus {csv_path} dropped {stats.rows_dropped_total}/{stats.raw_row_count} "
                f"rows ({drop_ratio:.1%}), above the {max_drop_ratio:.0%} ceiling "
                f"({stats.rows_missing_nct} missing NCT, {stats.duplicate_rows_dropped} duplicate "
                "NCT). Refusing to serve a degraded corpus."
            )


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


# ---------------------------------------------------------------------- cache
def _cache_path(csv_path: Path, cache_dir: str | Path | None) -> Path:
    base = Path(cache_dir) if cache_dir is not None else csv_path.parent / ".index_cache"
    return base / f"{csv_path.stem}.idx"


def _read_cache(cache_path: Path, content_hash: str) -> dict | None:
    """Return the cached payload, or None (with a logged reason) if it must be refused.

    The plaintext JSON header carrying the cache key is verified BEFORE the pickle
    payload is read, so a stale/foreign cache is never unpickled.
    """
    if not cache_path.exists():
        return None
    try:
        with cache_path.open("rb") as f:
            header_line = f.readline()
            if not header_line.strip():
                raise ValueError("missing cache header")
            header = json.loads(header_line.decode("utf-8"))
            if not isinstance(header, dict):
                raise ValueError("cache header is not an object")

            expected = {
                "cache_format_version": CACHE_FORMAT_VERSION,
                "content_hash": content_hash,
                "normalization_version": NORMALIZATION_VERSION,
            }
            mismatches = [
                f"{k}: cache={header.get(k)!r} expected={v!r}"
                for k, v in expected.items() if header.get(k) != v
            ]
            if mismatches:
                log.warning("REFUSING stale/incompatible index cache %s — %s. Rebuilding from CSV.",
                            cache_path, "; ".join(mismatches))
                return None

            payload = pickle.load(f)
    except Exception as exc:  # corrupt, truncated, unreadable, wrong pickle
        log.warning("REFUSING unreadable index cache %s (%s: %s). Rebuilding from CSV.",
                    cache_path, type(exc).__name__, exc)
        return None

    if (not isinstance(payload, dict)
            or not isinstance(payload.get("records"), list)
            or not payload["records"]
            or not isinstance(payload["records"][0], TrialRecord)
            or not isinstance(payload.get("load_stats"), dict)
            or not isinstance(payload.get("data_current_through"), str)):
        log.warning("REFUSING structurally invalid index cache %s. Rebuilding from CSV.", cache_path)
        return None

    log.info("loaded trial index from cache %s (%d records)", cache_path, len(payload["records"]))
    return payload


def _write_cache(cache_path: Path, content_hash: str, index: "TrialIndex") -> None:
    """Write the cache atomically. A cache we cannot write is a warning, never fatal."""
    header = {
        "cache_format_version": CACHE_FORMAT_VERSION,
        "content_hash": content_hash,
        "normalization_version": NORMALIZATION_VERSION,
    }
    payload = {
        "records": index.records,
        "data_current_through": index.data_current_through,
        "load_stats": index.load_stats.as_dict(),
    }
    # Unique temp name: two processes building the same corpus concurrently (CI, a test
    # run alongside a server boot) must not interleave writes into one temp file and
    # then rename the resulting mush into place.
    tmp = cache_path.with_suffix(f".idx.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("wb") as f:
            f.write(json.dumps(header).encode("utf-8") + b"\n")
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(cache_path)  # atomic: never leave a half-written cache in place
        log.info("wrote trial index cache %s", cache_path)
    except Exception as exc:
        log.warning("could not write index cache %s (%s: %s); continuing without it",
                    cache_path, type(exc).__name__, exc)
    finally:
        tmp.unlink(missing_ok=True)  # no-op after a successful replace()
