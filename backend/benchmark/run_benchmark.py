#!/usr/bin/env python3
"""Synthetic-EHR benchmark and release gate.

Runs the deterministic pipeline (rules extractor + rule reranker — no API key, no
network) against every case in benchmark/cases and scores it against that case's
COMMITTED expectation files. Deterministic and offline so CI can gate on it.

Design notes, because two earlier defects here were structural rather than clinical:

  * Expectations live in each case's `expected_profile.json` (`expected_extraction`,
    `expected_behavior`, `expected_retrieval`) and are EXECUTED. They used to be inert
    documentation next to a hardcoded table in this file — two ground-truth stores
    that drift. There is now one.

  * The gate asserts biomarker DIRECTION and that wrong disease families are ABSENT,
    not merely that the right one is present. The previous version checked only
    presence, so it passed 16/16 while the extractor read "ALK no rearrangement
    detected" as ALK POSITIVE and tagged every NSCLC patient with SCLC as well.

Metrics: recall@3, precision@10, wrong-disease@10, wrong-purpose@10, extraction
accuracy, caution recall, abstention correctness.

Run:  python benchmark/run_benchmark.py [--json out.json] [--baseline base.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.extraction.rules_extractor import RulesExtractor
from app.intake.deident import deidentify
from app.matching.pipeline import _abstention
from app.matching.rerank import DeterministicReranker
from app.trials.index import NORMALIZATION_VERSION, TrialIndex
from app.trials.retrieve import RetrievalFilters, patient_disease_families, retrieve

CASES = BACKEND / "benchmark" / "cases"
CSV = BACKEND / "data" / "trials.csv"

# Bump when a metric definition or threshold changes, so a score is never compared
# across incompatible scoring rules.
SCORING_VERSION = "2.0.0-executed-expectations"

NON_TREATMENT = {"observational", "diagnostic", "screening", "prevention",
                 "registry", "imaging", "supportive_care", "health_services_research",
                 "basic_science", "device_feasibility"}


# ----------------------------------------------------------------- helpers
def _text(item) -> str:
    """Reasons/cautions are Explanation objects; tolerate plain strings too."""
    return item if isinstance(item, str) else getattr(item, "text", str(item))


def _norm(s: str) -> str:
    return " ".join(str(s).lower().split())


class Scorecard:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def check(self, case: str, label: str, passed: bool, detail: str = "") -> bool:
        self.checks.append({"case": case, "label": label, "passed": bool(passed),
                            "detail": str(detail)[:300]})
        mark = "PASS" if passed else "FAIL"
        print(f"    [{mark}] {label}" + (f"  — {detail}" if detail else ""))
        return bool(passed)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def passed(self) -> int:
        return sum(c["passed"] for c in self.checks)

    def failures(self) -> list[dict]:
        return [c for c in self.checks if not c["passed"]]


# ----------------------------------------------------------------- extraction
def score_extraction(sc: Scorecard, cid: str, profile, fams: set[str], exp: dict) -> dict:
    """Assert the committed extraction expectations. Returns per-field tallies so an
    aggregate extraction-accuracy metric can be computed across cases."""
    hits = misses = 0

    def tally(ok: bool) -> bool:
        nonlocal hits, misses
        hits += int(ok)
        misses += int(not ok)
        return ok

    df = exp.get("disease_families", {})
    for want in df.get("present", []):
        tally(sc.check(cid, f"family present: {want!r}", want in fams, str(sorted(fams))))
    # Absence matters as much as presence: the NSCLC/SCLC substring bug was invisible
    # to a presence-only check.
    for bad in df.get("absent", []):
        tally(sc.check(cid, f"family ABSENT: {bad!r}", bad not in fams, str(sorted(fams))))

    if "is_metastatic" in exp:
        tally(sc.check(cid, f"is_metastatic == {exp['is_metastatic']}",
                       profile.is_metastatic == exp["is_metastatic"], str(profile.is_metastatic)))
    if exp.get("ecog") is not None:
        tally(sc.check(cid, f"ECOG == {exp['ecog']}", profile.ecog == exp["ecog"], str(profile.ecog)))

    sites = {s.lower() for s in profile.disease_sites}
    ms = exp.get("metastatic_sites", {})
    for want in ms.get("present", []):
        tally(sc.check(cid, f"met site present: {want}", want.lower() in sites, str(sorted(sites))))
    for bad in ms.get("absent", []):
        # Negated anatomy ("no focal liver lesion") must not become a metastatic site.
        tally(sc.check(cid, f"met site ABSENT: {bad}", bad.lower() not in sites, str(sorted(sites))))

    # Biomarker DIRECTION — the check whose absence let the ALK bug through.
    for want in exp.get("biomarkers", []):
        name, direction = want["name"], want["direction"]
        timing = want.get("timing")
        found = [b for b in profile.biomarkers if b.name.upper() == name.upper()
                 and (timing is None or getattr(b.timing, "value", b.timing) == timing)]
        got = sorted({b.status.value for b in found})
        label = f"biomarker {name}{f' [{timing}]' if timing else ''} == {direction}"
        tally(sc.check(cid, label, direction in got, f"got {got or 'MISSING'}"))

    for th in exp.get("therapies_present", []):
        names = {t.name.lower() for t in profile.therapies}
        tally(sc.check(cid, f"therapy present: {th}", th.lower() in names, str(sorted(names))))

    return {"hits": hits, "misses": misses}


# ----------------------------------------------------------------- retrieval
def score_behavior(sc: Scorecard, cid: str, profile, fams, index, reranker,
                   exp_behavior: dict, exp_retrieval: dict) -> dict:
    metrics: dict = {}
    needs_review, reasons = _abstention(profile)

    # A no-good-match case is satisfied by abstaining OR by returning nothing suitable.
    if exp_behavior.get("abstains_or_returns_nothing_suitable"):
        expect_abstain = None
    else:
        expect_abstain = exp_behavior.get("abstains", False)

    if expect_abstain is True:
        sc.check(cid, "abstains (needs-review)", needs_review, "; ".join(reasons))
        return {"abstained": True}
    if expect_abstain is False:
        sc.check(cid, "does NOT abstain", not needs_review, "; ".join(reasons))

    cands = retrieve(profile, index, filters=RetrievalFilters(
        active_only=False, interventional_only=False, treatment_only=True), top_k=60)
    results = reranker.rerank(profile, cands, 10)
    top10 = results[:10]
    metrics["n_results"] = len(results)

    # --- wrong-disease@10 -------------------------------------------------
    wrong_disease = []
    for r in top10:
        rec = index.get(r.nct)
        rec_fams = rec.disease_families if rec else frozenset()
        if rec_fams and fams and rec_fams.isdisjoint(fams) and not (rec and rec.is_basket):
            wrong_disease.append((r.nct, sorted(rec_fams)))
    metrics["wrong_disease_at_10"] = len(wrong_disease)
    sc.check(cid, f"wrong-disease@10 == {exp_behavior.get('wrong_disease_at_10', 0)}",
             len(wrong_disease) <= exp_behavior.get("wrong_disease_at_10", 0),
             str(wrong_disease[:3]))

    # --- wrong-purpose@10 -------------------------------------------------
    wrong_purpose = [(r.nct, r.study_purpose) for r in top10 if r.study_purpose in NON_TREATMENT]
    metrics["wrong_purpose_at_10"] = len(wrong_purpose)
    sc.check(cid, f"wrong-purpose@10 == {exp_behavior.get('wrong_purpose_at_10', 0)}",
             len(wrong_purpose) <= exp_behavior.get("wrong_purpose_at_10", 0),
             str(wrong_purpose[:3]))

    # --- precision@10 -----------------------------------------------------
    # "Relevant" = on-disease (shared family or an explicit basket) AND a treatment study.
    relevant = 0
    for r in top10:
        rec = index.get(r.nct)
        on_disease = bool(rec and (r.disease_family in fams or rec.is_basket
                                   or (rec.disease_families & fams)))
        relevant += int(on_disease and r.study_purpose not in NON_TREATMENT)
    metrics["precision_at_10"] = round(relevant / len(top10), 3) if top10 else None

    # --- recall@3 (the release gate) --------------------------------------
    anchors = exp_retrieval.get("must_rank_top_3", [])
    if anchors:
        top3 = [r.nct for r in results[:3]]
        found = [a for a in anchors if a in top3]
        metrics["recall_at_3"] = round(len(found) / len(anchors), 3)
        sc.check(cid, f"recall@3 == 1.0 (all of {anchors} in top 3)",
                 len(found) == len(anchors), f"top3={top3} missing={set(anchors)-set(found)}")

    # --- contraindication must never rank first ---------------------------
    if exp_behavior.get("no_contraindicated_rank_1") and results:
        sc.check(cid, "rank 1 carries no contraindication",
                 not results[0].contraindications,
                 str([_text(c) for c in results[0].contraindications][:2]))

    # --- caution recall ---------------------------------------------------
    expected_cautions = exp_behavior.get("expected_cautions", [])
    if expected_cautions:
        blob = _norm(" ".join(_text(c) for r in results for c in list(r.cautions) + list(r.contraindications)))
        found = [c for c in expected_cautions if _norm(c) in blob]
        metrics["caution_recall"] = round(len(found) / len(expected_cautions), 3)
        sc.check(cid, f"caution recall ({len(found)}/{len(expected_cautions)})",
                 len(found) == len(expected_cautions),
                 f"missing={[c for c in expected_cautions if c not in found]}")

    # --- no-good-match specifics ------------------------------------------
    if exp_behavior.get("abstains_or_returns_nothing_suitable"):
        only_baskets = all((index.get(r.nct).is_basket if index.get(r.nct) else False)
                           for r in top10) if top10 else True
        ok = needs_review or not top10 or only_baskets
        sc.check(cid, "no-good-match: abstains / no results / baskets only", ok,
                 f"needs_review={needs_review} n={len(top10)} only_baskets={only_baskets}")
    if exp_behavior.get("max_match_score") is not None and top10:
        cap = exp_behavior["max_match_score"]
        top_score = max(r.match_score for r in top10)
        metrics["max_match_score"] = top_score
        sc.check(cid, f"no confident match (max score < {cap})", top_score < cap, f"max={top_score}")

    # --- nothing may CLAIM evidence it does not have ----------------------
    # An explanation carrying a quote must be grounded in the record. Synthesized
    # statements with no quote are legitimate (e.g. "no listed study site in Ohio"
    # for a trial that lists no sites at all — there is nothing to quote); they are
    # only a defect if they present themselves as evidence.
    unfounded = [(_text(e), getattr(e, "source_field", "")) for r in top10
                 for e in list(r.reasons) + list(r.cautions)
                 if getattr(e, "evidence_snippet", "") and getattr(e, "grounded", True) is False]
    sc.check(cid, "no explanation claims evidence it lacks", not unfounded, str(unfounded[:2]))

    return metrics


# ----------------------------------------------------------------- main
def run(json_out: Path | None = None, baseline: Path | None = None) -> int:
    if not CSV.exists():
        print("trials.csv not present; cannot run benchmark.")
        return 2
    print(f"Loading index from {CSV.name} ...")
    index = TrialIndex.from_csv(CSV)
    reranker = DeterministicReranker()
    manifest = index.manifest()
    sc = Scorecard()
    per_case: dict[str, dict] = {}
    extraction_hits = extraction_misses = 0

    for case_dir in sorted(CASES.iterdir()):
        if not case_dir.is_dir():
            continue
        cid = case_dir.name
        note_path = case_dir / "source_note.txt"
        prof_path = case_dir / "expected_profile.json"
        if not note_path.exists() or not prof_path.exists():
            print(f"\n=== {cid} ===\n    SKIPPED: missing source_note.txt or expected_profile.json")
            sc.check(cid, "case is runnable (has note + expectations)", False, "missing files")
            continue

        expected = json.loads(prof_path.read_text(encoding="utf-8"))
        note = note_path.read_text(encoding="utf-8", errors="ignore")
        profile = RulesExtractor().extract(deidentify(note).text)
        fams = set(patient_disease_families(profile))

        print(f"\n=== {cid}  [{expected.get('case_type', '?')}] ===")
        print(f"    extracted: families={sorted(fams)} sites={profile.disease_sites} "
              f"biomarkers={[(b.name, b.status.value) for b in profile.biomarkers]}")

        tally = score_extraction(sc, cid, profile, fams, expected.get("expected_extraction", {}))
        extraction_hits += tally["hits"]
        extraction_misses += tally["misses"]

        per_case[cid] = score_behavior(
            sc, cid, profile, fams, index, reranker,
            expected.get("expected_behavior", {}),
            expected.get("expected_retrieval", {}),
        )
        per_case[cid]["case_type"] = expected.get("case_type")

    denom = extraction_hits + extraction_misses
    extraction_accuracy = round(extraction_hits / denom, 3) if denom else None

    report = {
        "scoring_version": SCORING_VERSION,
        "versions": {
            # Every axis the requirements doc asks a run to be reproducible against.
            "dataset": {
                "content_hash": manifest.get("content_hash"),
                "row_count": manifest.get("row_count"),
                "data_current_through": manifest.get("data_current_through"),
                "schema_fingerprint": manifest.get("schema_fingerprint"),
            },
            "index": {"normalization_version": NORMALIZATION_VERSION,
                      "built_at": manifest.get("built_at")},
            "extractor": RulesExtractor.name,
            "reranker": "deterministic",
            # Deterministic path only: no model or prompt is exercised. Stated
            # explicitly so a run is never mistaken for covering the LLM path.
            "model": None,
            "prompt": None,
        },
        "totals": {"checks": sc.total, "passed": sc.passed, "failed": sc.total - sc.passed},
        "metrics": {"extraction_accuracy": extraction_accuracy,
                    "extraction_checks": denom, **{k: v for k, v in per_case.items()}},
        "failures": sc.failures(),
    }

    print(f"\n{'=' * 60}")
    print(f"extraction accuracy : {extraction_accuracy}  ({extraction_hits}/{denom} field checks)")
    for cid, m in per_case.items():
        bits = [f"{k}={v}" for k, v in m.items()
                if k in {"recall_at_3", "precision_at_10", "wrong_disease_at_10",
                         "wrong_purpose_at_10", "caution_recall"}]
        if bits:
            print(f"  {cid}: {'  '.join(bits)}")
    print(f"\nSCORECARD: {sc.passed}/{sc.total} checks passed")
    print(f"corpus {manifest.get('content_hash')} | norm {NORMALIZATION_VERSION} | scoring {SCORING_VERSION}")

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {json_out}")

    if baseline and baseline.exists():
        base = json.loads(baseline.read_text(encoding="utf-8"))
        b_pass = base.get("totals", {}).get("passed")
        if b_pass is not None and sc.passed < b_pass:
            print(f"REGRESSION vs baseline: {sc.passed} < {b_pass} checks passed")
            return 1
        print(f"baseline OK ({sc.passed} >= {b_pass} passed)")

    if sc.failures():
        print("\nFAILED CHECKS:")
        for f in sc.failures():
            print(f"  - [{f['case']}] {f['label']}  {f['detail']}")
    return 0 if sc.passed == sc.total else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, default=None, help="write a machine-readable scorecard")
    ap.add_argument("--baseline", type=Path, default=None, help="fail if worse than this scorecard")
    a = ap.parse_args()
    raise SystemExit(run(json_out=a.json, baseline=a.baseline))
