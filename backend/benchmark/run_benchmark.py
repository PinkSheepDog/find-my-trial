#!/usr/bin/env python3
"""Feedback benchmark — runs the CURRENT deterministic pipeline against the 4
synthetic EHR cases and prints a pass/fail scorecard for the P0 behaviors:

  * disease-family extraction         (right primary cancer identified)
  * abstention                        (incomplete case -> needs-review, not a list)
  * wrong-disease@10 == 0             (no other-cancer study in the top 10)
  * purpose gate                      (no observational/diagnostic study for a treatment query)
  * negation                          (negated anatomy not stored as metastatic sites)

Deterministic + offline (rules extractor + rule reranker) so it is reproducible in CI.
Run:  python benchmark/run_benchmark.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.extraction.rules_extractor import RulesExtractor
from app.intake.deident import deidentify
from app.matching.pipeline import _abstention
from app.matching.rerank import DeterministicReranker
from app.trials.index import TrialIndex
from app.trials.retrieve import RetrievalFilters, patient_disease_families, retrieve

CASES = BACKEND / "benchmark" / "cases"
CSV = BACKEND / "data" / "trials.csv"

# Cancers that must NOT appear in the lung case's top results via biomarker overlap.
_WRONG_FOR_LUNG = {"breast cancer", "melanoma", "gastric cancer", "biliary tract cancer"}
_EXPECTED_FAMILY = {
    "case_01_clean_her2_positive_breast": "breast cancer",
    "case_02_messy_tnbc_her2_low": "breast cancer",
    "case_03_nsclc_egfr_negative_control": "non-small cell lung cancer",
    "case_04_incomplete_pancreatic_abstention": "pancreatic cancer",
}


def _check(label: str, passed: bool, detail: str = "") -> tuple[str, bool]:
    mark = "PASS" if passed else "FAIL"
    print(f"    [{mark}] {label}" + (f"  — {detail}" if detail else ""))
    return (label, passed)


def run() -> int:
    if not CSV.exists():
        print("trials.csv not present; cannot run benchmark.")
        return 2
    print(f"Loading index from {CSV.name} ...")
    index = TrialIndex.from_csv(CSV)
    reranker = DeterministicReranker()
    total = passed = 0

    for case_dir in sorted(CASES.iterdir()):
        if not case_dir.is_dir():
            continue
        cid = case_dir.name
        note = (case_dir / "source_note.txt").read_text(encoding="utf-8", errors="ignore")
        profile = RulesExtractor().extract(deidentify(note).text)
        fams = patient_disease_families(profile)
        needs_review, reasons = _abstention(profile)
        print(f"\n=== {cid} ===")
        print(f"    extracted: families={sorted(fams)} sites={profile.disease_sites} "
              f"biomarkers={[(b.name, b.status.value) for b in profile.biomarkers]}")

        checks: list[tuple[str, bool]] = []
        exp_fam = _EXPECTED_FAMILY[cid]
        checks.append(_check(f"disease family = {exp_fam!r}", exp_fam in fams, str(sorted(fams))))

        if cid.startswith("case_04"):
            checks.append(_check("abstains (needs-review)", needs_review, "; ".join(reasons)))
        else:
            checks.append(_check("does NOT abstain", not needs_review, "; ".join(reasons)))
            cands = retrieve(profile, index, filters=RetrievalFilters(
                active_only=False, interventional_only=False, treatment_only=True), top_k=40)
            results = reranker.rerank(profile, cands, 10)
            # wrong-disease@10
            wrong = []
            for r in results:
                rec = index.get(r.nct)
                rec_fams = rec.disease_families if rec else frozenset()
                if rec_fams and fams and rec_fams.isdisjoint(fams) and not (rec and rec.is_basket):
                    wrong.append((r.nct, sorted(rec_fams)))
            checks.append(_check("wrong-disease@10 == 0", not wrong, str(wrong[:3])))
            # purpose gate: no observational/diagnostic/screening in top 10
            bad_purpose = [(r.nct, r.study_purpose) for r in results
                           if r.study_purpose in {"observational", "diagnostic", "screening", "prevention"}]
            checks.append(_check("purpose gate holds (top 10)", not bad_purpose, str(bad_purpose[:3])))
            if cid.startswith("case_03") and results:
                lung_wrong = [w for w in wrong if set(w[1]) & _WRONG_FOR_LUNG]
                checks.append(_check("no breast/melanoma/gastric/biliary via PD-L1", not lung_wrong, str(lung_wrong[:3])))

        if cid.startswith("case_02"):
            neg_ok = not ({"liver", "bone"} & {s.lower() for s in profile.disease_sites})
            checks.append(_check("negation: liver/bone NOT metastatic sites", neg_ok, str(profile.disease_sites)))

        for _, ok in checks:
            total += 1
            passed += int(ok)

    print(f"\n{'='*50}\nSCORECARD: {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(run())
