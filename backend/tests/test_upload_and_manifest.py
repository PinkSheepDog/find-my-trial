"""Upload validation (extension + magic-byte signature + encrypted-PDF reject) and
index build manifest/versioning (feedback: safe uploads + versioned index)."""
from __future__ import annotations

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


@pytest.mark.parametrize("name,data,code", [
    ("empty.txt", b"", 400),
    ("malware.exe", b"MZ...", 415),
    ("fake.pdf", b"not a real pdf", 415),          # extension/signature mismatch
    ("locked.pdf", b"%PDF-1.7\n/Encrypt 1 0 R", 415),
])
def test_rejects_bad_uploads(name, data, code):
    with pytest.raises(UploadRejected) as ei:
        validate_upload(name, data)
    assert ei.value.status_code == code


@pytest.mark.skipif(not CSV.exists(), reason="trial CSV not present")
def test_index_manifest_fields():
    from app.trials.index import NORMALIZATION_VERSION, TrialIndex
    m = TrialIndex.from_csv(CSV).manifest()
    assert m["row_count"] > 0
    assert m["normalization_version"] == NORMALIZATION_VERSION
    assert len(m["content_hash"]) == 16
    assert m["built_at"] and m["data_current_through"]
