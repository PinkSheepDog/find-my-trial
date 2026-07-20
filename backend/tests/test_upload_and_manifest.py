"""Upload validation (extension + magic-byte signature + encrypted-PDF reject) and
index build manifest/versioning (feedback: safe uploads + versioned index)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.intake.extract_text import UploadRejected, validate_upload

BACKEND = Path(__file__).resolve().parent.parent
CSV = BACKEND / "data" / "trials.csv"


def test_accepts_valid_uploads():
    validate_upload("note.txt", b"64F metastatic breast cancer")
    validate_upload("chart.pdf", b"%PDF-1.7\n...")
    validate_upload("doc.docx", b"PK\x03\x04rest-of-zip")
    validate_upload("bundle.json", b'{"resourceType":"Bundle"}')


def test_accepts_pdf_with_leading_bom_or_junk():
    """The validator must not be stricter than the PDF reader: a %PDF marker that
    follows a BOM or a few leading bytes is still a readable PDF (PyMuPDF opens it),
    so it must pass validation rather than be rejected as content-mismatch."""
    validate_upload("bom.pdf", b"\xef\xbb\xbf%PDF-1.5\n...")           # UTF-8 BOM before marker
    validate_upload("lead.pdf", b"   \r\n%PDF-1.4 rest of file")        # leading whitespace
    validate_upload("scan.pdf", b"\x00\x00garbage\n%PDF-1.7 scanned")   # junk within first KB


@pytest.mark.parametrize("name,data,code", [
    ("empty.txt", b"", 400),
    ("malware.exe", b"MZ...", 415),
    ("fake.pdf", b"not a real pdf", 415),          # extension/signature mismatch
    ("no-marker.pdf", b"x" * 4096, 415),           # no %PDF anywhere in the scan window
    ("locked.pdf", b"%PDF-1.7\n/Encrypt 1 0 R", 415),
])
def test_rejects_bad_uploads(name, data, code):
    with pytest.raises(UploadRejected) as ei:
        validate_upload(name, data)
    assert ei.value.status_code == code


@pytest.mark.skipif(not CSV.exists(), reason="trial CSV not present")
def test_index_manifest_fields():
    from app.trials.index import NORMALIZATION_VERSION, TrialIndex
    m = TrialIndex.from_csv(CSV, use_cache=False).manifest()
    assert m["row_count"] > 0
    assert m["normalization_version"] == NORMALIZATION_VERSION
    # 32 hex chars (128 bits) — 16 is too weak to rely on as an integrity fingerprint.
    assert len(m["content_hash"]) == 32
    assert m["built_at"] and m["data_current_through"]


# --------------------------------------------------------------------------- corpus loading
# The real corpus is clean (no duplicate NCTs, no blank ids, all ISO dates), so the
# defensive loading paths are exercised against synthetic CSVs written to tmp_path.

REQUIRED = [
    "NCT Number", "Study Title", "Study URL", "Study Status", "Brief Summary",
    "Conditions", "Interventions", "Locations", "Sponsor", "Sex", "Age",
    "Phases", "Study Type", "Study Design", "Last Update Posted",
]


def _row(nct: str, *, updated: str = "2024-01-15", title: str = "A Study of Widgetinib",
         **over) -> dict:
    row = {c: "" for c in REQUIRED}
    row.update({
        "NCT Number": nct, "Study Title": title,
        "Study URL": f"https://clinicaltrials.gov/study/{nct}",
        "Study Status": "RECRUITING", "Brief Summary": "A trial of widgetinib.",
        "Conditions": "Breast Cancer", "Interventions": "DRUG: Widgetinib",
        "Locations": "Boston, MA", "Sponsor": "Acme", "Sex": "ALL",
        "Age": "ADULT, OLDER_ADULT", "Phases": "PHASE2", "Study Type": "INTERVENTIONAL",
        "Study Design": "Allocation: RANDOMIZED|Primary Purpose: TREATMENT",
        "Last Update Posted": updated,
    })
    row.update(over)
    return row


def _write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> Path:
    import csv as _csv
    cols = columns if columns is not None else REQUIRED
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def _build(path: Path, **kw):
    """Build an index from a small synthetic corpus (floors relaxed for fixtures)."""
    from app.trials.index import TrialIndex
    kw.setdefault("use_cache", False)
    kw.setdefault("min_rows", 1)
    return TrialIndex.from_csv(path, **kw)


def test_missing_required_column_raises_loudly(tmp_path):
    """Defect 2: a CSV missing an entire column used to load silently with blank fields."""
    from app.trials.index import CorpusSchemaError

    cols = [c for c in REQUIRED if c not in ("Conditions", "Sponsor")]
    _write_csv(tmp_path / "t.csv", [_row(f"NCT{i:08d}") for i in range(5)], columns=cols)

    with pytest.raises(CorpusSchemaError) as ei:
        _build(tmp_path / "t.csv")
    msg = str(ei.value)
    assert "Conditions" in msg and "Sponsor" in msg, "error must name the missing columns"


def test_duplicate_ncts_are_deduped_keeping_most_recent(tmp_path):
    """Defect 1: duplicates were scored/returned twice but resolved to one via get()."""
    _write_csv(tmp_path / "t.csv", [
        _row("NCT00000001", updated="2020-01-01", title="Old title"),
        _row("NCT00000002", updated="2021-06-01"),
        _row("NCT00000001", updated="2024-09-09", title="New title"),  # newer duplicate wins
        _row("NCT00000001", updated="2019-01-01", title="Older title"),
    ])
    # This fixture is deliberately 50% duplicates, which would trip the drop ceiling.
    idx = _build(tmp_path / "t.csv", max_drop_ratio=1.0)

    assert len(idx.records) == 2, "duplicate NCTs must collapse to one record"
    assert len({r.nct for r in idx.records}) == 2
    assert idx.get("NCT00000001").title == "New title", "most-recently-updated row must win"

    m = idx.manifest()
    assert m["duplicate_rows_dropped"] == 2
    assert m["raw_row_count"] == 4 and m["row_count"] == 2
    assert m["rows_dropped_total"] == 2
    # records and the NCT lookup must agree — the original inconsistency
    assert len(idx.records) == len(idx._by_nct)


def test_rows_without_nct_are_counted_not_silently_discarded(tmp_path):
    _write_csv(tmp_path / "t.csv", [
        _row("NCT00000001"), _row(""), _row("   "), _row("NCT00000002"),
    ])
    idx = _build(tmp_path / "t.csv", max_drop_ratio=1.0)
    m = idx.manifest()
    assert m["rows_missing_nct"] == 2
    assert m["row_count"] == 2 and m["raw_row_count"] == 4


def test_malformed_dates_skipped_and_counted(tmp_path):
    """Defect 4: data_current_through was a lexicographic max over raw strings."""
    _write_csv(tmp_path / "t.csv", [
        _row("NCT00000001", updated="2021-03-04"),
        _row("NCT00000002", updated="not a date"),
        _row("NCT00000003", updated=""),
        _row("NCT00000004", updated="2023-11-30"),
    ])
    idx = _build(tmp_path / "t.csv")
    m = idx.manifest()
    assert m["data_current_through"] == "2023-11-30"
    assert m["unparseable_dates"] == 1
    assert m["missing_dates"] == 1


def test_us_style_date_does_not_corrupt_data_current_through(tmp_path):
    """9/9/2025 sorts BEFORE 2021-03-04 lexicographically but is genuinely later."""
    _write_csv(tmp_path / "t.csv", [
        _row("NCT00000001", updated="2021-03-04"),
        _row("NCT00000002", updated="9/9/2025"),
    ])
    m = _build(tmp_path / "t.csv").manifest()
    assert m["data_current_through"] == "2025-09-09"
    assert m["unparseable_dates"] == 0


def test_manifest_exposes_schema_fingerprint_and_counters(tmp_path):
    """Defect 5: a column-set change was previously undetectable from the manifest."""
    from app.trials.index import NORMALIZATION_VERSION

    rows = [_row(f"NCT{i:08d}") for i in range(4)]
    m = _build(_write_csv(tmp_path / "a.csv", rows)).manifest()

    for key in ("row_count", "raw_row_count", "content_hash", "schema_fingerprint",
                "schema_column_count", "normalization_version", "data_current_through",
                "built_at", "rows_missing_nct", "duplicate_rows_dropped",
                "rows_dropped_total", "unparseable_dates", "missing_dates",
                "loaded_from_cache"):
        assert key in m, f"manifest missing {key}"

    assert m["normalization_version"] == NORMALIZATION_VERSION
    assert len(m["content_hash"]) == 32
    assert m["schema_column_count"] == len(REQUIRED)

    # An added column must move the schema fingerprint.
    m2 = _build(_write_csv(tmp_path / "b.csv", rows, columns=REQUIRED + ["Acronym"])).manifest()
    assert m2["schema_fingerprint"] != m["schema_fingerprint"]
    assert m2["schema_column_count"] == len(REQUIRED) + 1


def test_empty_corpus_raises_clearly(tmp_path):
    """Defect 3: BM25Okapi([]) crashed with a ZeroDivisionError on avg doc length."""
    from app.trials.index import EmptyCorpusError, TrialIndex

    _write_csv(tmp_path / "t.csv", [])
    with pytest.raises(EmptyCorpusError) as ei:
        _build(tmp_path / "t.csv")
    assert "empty" in str(ei.value).lower()

    # Direct construction is guarded too — no opaque ZeroDivisionError.
    with pytest.raises(EmptyCorpusError):
        TrialIndex([], "deadbeef")


def test_corpus_of_only_blank_ncts_raises(tmp_path):
    from app.trials.index import EmptyCorpusError
    _write_csv(tmp_path / "t.csv", [_row(""), _row("")])
    with pytest.raises(EmptyCorpusError):
        _build(tmp_path / "t.csv", max_drop_ratio=1.0)


def test_row_floor_rejects_degraded_corpus(tmp_path):
    from app.trials.index import CorpusQualityError
    _write_csv(tmp_path / "t.csv", [_row(f"NCT{i:08d}") for i in range(3)])
    with pytest.raises(CorpusQualityError) as ei:
        _build(tmp_path / "t.csv", min_rows=100)
    assert "100" in str(ei.value)


def test_drop_ratio_ceiling_rejects_degraded_corpus(tmp_path):
    """Half the rows unusable must fail loudly, not ship a half-empty index."""
    from app.trials.index import CorpusQualityError
    rows = [_row(f"NCT{i:08d}") for i in range(10)] + [_row("") for _ in range(10)]
    _write_csv(tmp_path / "t.csv", rows)
    with pytest.raises(CorpusQualityError) as ei:
        _build(tmp_path / "t.csv", max_drop_ratio=0.10)
    assert "50.0%" in str(ei.value) and "Refusing" in str(ei.value)


# --------------------------------------------------------------------------- on-disk cache
def _cache_file(tmp_path: Path) -> Path:
    return tmp_path / ".index_cache" / "t.idx"


def test_cache_roundtrip_matches_fresh_build(tmp_path):
    """A cache hit must be indistinguishable from a fresh parse."""
    csv = _write_csv(tmp_path / "t.csv", [_row(f"NCT{i:08d}", updated="2024-0%d-01" % (i % 9 + 1))
                                          for i in range(6)])
    fresh = _build(csv, use_cache=True)
    assert fresh.loaded_from_cache is False
    assert _cache_file(tmp_path).exists(), "cache should have been written"

    cached = _build(csv, use_cache=True)
    assert cached.loaded_from_cache is True
    assert [r.nct for r in cached.records] == [r.nct for r in fresh.records]
    assert cached.data_current_through == fresh.data_current_through
    assert cached.stats() == fresh.stats()
    for key, val in fresh.manifest().items():
        if key in ("built_at", "loaded_from_cache"):
            continue
        assert cached.manifest()[key] == val, f"cached manifest diverged on {key}"
    # BM25 was rebuilt from cached tokens and still scores.
    from app.trials.index import _tokenize
    assert cached.bm25_scores(_tokenize("widgetinib breast")).shape[0] == len(cached.records)


def test_stale_cache_is_refused_when_corpus_changes(tmp_path, caplog):
    """The P0 requirement: an in-place corpus swap must never serve the old index."""
    csv = tmp_path / "t.csv"
    _write_csv(csv, [_row(f"NCT{i:08d}") for i in range(4)])
    first = _build(csv, use_cache=True)
    assert first.loaded_from_cache is False

    # Swap the corpus in place — same path, different content.
    _write_csv(csv, [_row(f"NCT{i:08d}") for i in range(7)])
    with caplog.at_level("WARNING"):
        second = _build(csv, use_cache=True)

    assert second.loaded_from_cache is False, "stale cache was served after a corpus swap"
    assert len(second.records) == 7
    assert "REFUSING" in caplog.text and "content_hash" in caplog.text

    # And the refreshed cache is now the one that gets served.
    third = _build(csv, use_cache=True)
    assert third.loaded_from_cache is True and len(third.records) == 7


def test_cache_refused_on_normalization_version_mismatch(tmp_path, caplog):
    csv = _write_csv(tmp_path / "t.csv", [_row(f"NCT{i:08d}") for i in range(4)])
    _build(csv, use_cache=True)

    cache = _cache_file(tmp_path)
    raw = cache.read_bytes()
    header_line, payload = raw.split(b"\n", 1)
    header = json.loads(header_line)
    header["normalization_version"] = "0.0.1-ancient"
    cache.write_bytes(json.dumps(header).encode() + b"\n" + payload)

    with caplog.at_level("WARNING"):
        idx = _build(csv, use_cache=True)
    assert idx.loaded_from_cache is False
    assert "REFUSING" in caplog.text and "normalization_version" in caplog.text


def test_cache_refused_on_format_version_mismatch(tmp_path, caplog):
    csv = _write_csv(tmp_path / "t.csv", [_row(f"NCT{i:08d}") for i in range(4)])
    _build(csv, use_cache=True)

    cache = _cache_file(tmp_path)
    header_line, payload = cache.read_bytes().split(b"\n", 1)
    header = json.loads(header_line)
    header["cache_format_version"] = 999
    cache.write_bytes(json.dumps(header).encode() + b"\n" + payload)

    with caplog.at_level("WARNING"):
        assert _build(csv, use_cache=True).loaded_from_cache is False
    assert "cache_format_version" in caplog.text


@pytest.mark.parametrize("corrupt", [
    b"",                                   # empty file
    b"not json at all\ngarbage",           # unreadable header
    b"{}\n",                               # header present, no payload
])
def test_corrupt_cache_is_refused_and_rebuilds(tmp_path, caplog, corrupt):
    csv = _write_csv(tmp_path / "t.csv", [_row(f"NCT{i:08d}") for i in range(4)])
    _build(csv, use_cache=True)
    _cache_file(tmp_path).write_bytes(corrupt)

    with caplog.at_level("WARNING"):
        idx = _build(csv, use_cache=True)
    assert idx.loaded_from_cache is False, "corrupt cache must not be served"
    assert len(idx.records) == 4, "must fall back to a clean rebuild"


def test_cache_with_valid_header_but_junk_payload_is_refused(tmp_path, caplog):
    """A truncated pickle behind a valid header must not take the process down."""
    from app.trials.index import CACHE_FORMAT_VERSION, NORMALIZATION_VERSION, _hash_file

    csv = _write_csv(tmp_path / "t.csv", [_row(f"NCT{i:08d}") for i in range(4)])
    cache = _cache_file(tmp_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "cache_format_version": CACHE_FORMAT_VERSION,
        "content_hash": _hash_file(csv),
        "normalization_version": NORMALIZATION_VERSION,
    }
    cache.write_bytes(json.dumps(header).encode() + b"\n" + b"\x80\x05 truncated pickle")

    with caplog.at_level("WARNING"):
        idx = _build(csv, use_cache=True)
    assert idx.loaded_from_cache is False and len(idx.records) == 4
    assert "REFUSING" in caplog.text


def test_use_cache_false_never_reads_or_writes(tmp_path):
    csv = _write_csv(tmp_path / "t.csv", [_row(f"NCT{i:08d}") for i in range(4)])
    idx = _build(csv, use_cache=False)
    assert idx.loaded_from_cache is False
    assert not _cache_file(tmp_path).exists()


def test_cache_write_leaves_no_temp_files(tmp_path):
    """Temp files are uniquely named (so concurrent builders cannot interleave writes
    into one file) and must be cleaned up on both success and failure."""
    csv = _write_csv(tmp_path / "t.csv", [_row(f"NCT{i:08d}") for i in range(4)])
    _build(csv, use_cache=True)
    _build(csv, use_cache=True)
    leftovers = list((tmp_path / ".index_cache").glob("*.tmp"))
    assert not leftovers, f"temp files leaked: {leftovers}"


def test_get_index_helper_is_gone():
    """Defect 6: the unused @lru_cache helper keyed on path string, not content hash —
    it would have served a stale index after an in-place file swap. Deleted."""
    import app.trials.index as index_mod
    assert not hasattr(index_mod, "get_index")
