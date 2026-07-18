"""Release gate: the synthetic-EHR benchmark must stay green (feedback P0 #2 —
block releases on known-case regressions)."""
from __future__ import annotations

from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
CSV = BACKEND / "data" / "trials.csv"

pytestmark = pytest.mark.skipif(not CSV.exists(), reason="trial CSV not present")


def test_synthetic_ehr_benchmark_all_checks_pass():
    from benchmark.run_benchmark import run
    assert run() == 0, "benchmark regressed — see the scorecard above"
