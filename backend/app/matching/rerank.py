"""Reranking + explanation — turns candidates into physician-facing trial cards.

Two interchangeable paths producing the SAME TrialResult schema:

  * Deterministic reranker (always available): builds reasons, cautions, a
    calibrated confidence, and a transparent score breakdown from the structured
    signals already computed in retrieval. No double-counting, single clip, one
    documented formula. Works fully offline.

  * LLM reranker (when an OpenRouter key is present): sends the small candidate set
    + de-identified profile to the model and asks for clinical reasoning — the
    grade-3-hepatitis -> checkpoint-exclusion style cautions that only an LLM does
    well — returning the same schema. Falls back to deterministic on any failure.

Confidence is fit, not eligibility. It is bounded, monotonic in signal, and
penalized by contraindications and inactive status — never a black box.
"""

from __future__ import annotations

from app.config import Settings
from app.extraction.schema import PatientProfile
from app.llm.openrouter import LLMUnavailable, OpenRouterClient
from app.matching.results import ScoreBreakdown, TrialResult
from app.trials.index import RECRUITING_STATUSES
from app.trials.retrieve import Candidate

# Confidence weights (sum of positive contributions = 100 at theoretical max).
_W_CONDITION = 38.0
_W_BIOMARKER = 24.0
_W_THERAPY = 10.0
_W_LEXICAL = 16.0
_W_STATUS = 12.0


def _status_factor(status: str) -> float:
    if status in RECRUITING_STATUSES:
        return 1.0
    if status == "ACTIVE_NOT_RECRUITING":
        return 0.55
    if status in {"UNKNOWN", ""}:
        return 0.4
    return 0.15  # completed / terminated / withdrawn


def _fit_label(conf: float, contraindicated: bool) -> str:
    if contraindicated:
        return "Conflicting requirement"
    if conf >= 80:
        return "Strong fit"
    if conf >= 60:
        return "Promising"
    if conf >= 40:
        return "Conditional"
    return "Low fit"


class DeterministicReranker:
    name = "rules"

    def rerank(self, profile: PatientProfile, candidates: list[Candidate], top_k: int) -> list[TrialResult]:
        scored: list[tuple[float, Candidate, ScoreBreakdown]] = []
        for c in candidates:
            cond = c.structured_overlap if c.matched_conditions else 0.0
            cond_pts = _W_CONDITION * (len(c.matched_conditions) / max(len(profile.cancer_types), 1)) if profile.cancer_types else 0.0
            bio_pts = _W_BIOMARKER * (len(c.matched_biomarkers) / max(len(profile.positive_biomarkers()), 1)) if profile.positive_biomarkers() else 0.0
            ther_pts = _W_THERAPY * min(len(c.matched_therapies) / max(len(profile.therapies), 1), 1.0) if profile.therapies else 0.0
            lex_pts = _W_LEXICAL * c.bm25
            status_pts = _W_STATUS * _status_factor(c.record.status)

            raw = cond_pts + bio_pts + ther_pts + lex_pts + status_pts
            penalty = 0.0
            if c.contraindications:
                penalty = raw * 0.65  # heavy, transparent demotion (not deletion)
            confidence = max(0.0, min(100.0, raw - penalty))

            breakdown = ScoreBreakdown(
                condition=round(cond_pts, 1), biomarker=round(bio_pts, 1),
                therapy=round(ther_pts, 1), lexical=round(lex_pts, 1),
                status=round(status_pts, 1), contraindication_penalty=round(-penalty, 1),
            )
            scored.append((confidence, c, breakdown))

        scored.sort(key=lambda t: t[0], reverse=True)
        results: list[TrialResult] = []
        for rank, (conf, c, breakdown) in enumerate(scored[:top_k], start=1):
            results.append(self._to_result(rank, conf, c, breakdown, profile))
        return results

    def _to_result(self, rank, conf, c: Candidate, breakdown, profile) -> TrialResult:
        rec = c.record
        reasons = self._reasons(c, profile)
        cautions = self._cautions(c, profile)
        return TrialResult(
            rank=rank, nct=rec.nct, title=rec.title, url=rec.url,
            status=rec.status.replace("_", " ").title(), phase=rec.phase or "Not specified",
            study_type=rec.study_type.replace("_", " ").title(), sponsor=rec.sponsor,
            brief_summary=rec.brief_summary[:420],
            conditions=rec.conditions[:6], interventions=rec.interventions[:6],
            locations=rec.locations[:3],
            match_score=round(conf, 1),
            fit_label=_fit_label(conf, bool(c.contraindications)),
            reasons=reasons, cautions=cautions, contraindications=c.contraindications,
            matched_conditions=c.matched_conditions, matched_biomarkers=c.matched_biomarkers,
            matched_therapies=c.matched_therapies, breakdown=breakdown,
            eligibility_sex=rec.sex if rec.sex not in {"NA", ""} else "Not specified",
            eligibility_age=", ".join(sorted(rec.age_buckets)) if rec.age_buckets else "Not specified",
            disease_family=c.disease_family, study_purpose=c.study_purpose,
            explained_by=self.name,
        )

    def _reasons(self, c: Candidate, profile: PatientProfile) -> list[str]:
        out = []
        # Cite disease, purpose and status evidence first (the gate signals).
        if c.disease_family:
            out.append("Primary disease match: " + c.disease_family)
        elif c.record.is_basket:
            out.append("Tumour-agnostic / basket study")
        if c.study_purpose and c.study_purpose not in {"unknown", "treatment"}:
            out.append("Study purpose: " + c.study_purpose.replace("_", " "))
        elif c.study_purpose == "treatment":
            out.append("Treatment study")
        if c.matched_biomarkers:
            out.append("Biomarker target overlap: " + ", ".join(c.matched_biomarkers[:3]))
        if c.matched_therapies:
            out.append("Relevant therapy class: " + ", ".join(c.matched_therapies[:3]))
        if c.record.is_recruiting:
            out.append("Currently recruiting — " + c.record.status.replace("_", " ").title())
        return out[:5]

    def _cautions(self, c: Candidate, profile: PatientProfile) -> list[str]:
        out = []
        if not c.record.is_recruiting:
            out.append(f"Status is {c.record.status.replace('_', ' ').title()} — may limit usefulness")
        # Safety-aware cautions derived from the structured profile.
        if profile.ecog is not None and profile.ecog >= 2:
            out.append(f"ECOG {profile.ecog} may fail protocol performance-status thresholds")
        for t in profile.therapies:
            if t.caused_toxicity and any(
                k in c.record.search_text.lower() for k in ("immunotherapy", "checkpoint", "pd-1", "pd-l1", "atezolizumab", "pembrolizumab")
            ):
                out.append(f"Prior {t.name} caused {t.caused_toxicity} — verify checkpoint-therapy exclusions")
                break
        if "LFT elevation" in profile.organ_function_flags:
            out.append("LFT elevation present — verify hepatic-function eligibility thresholds")
        if not c.record.phase:
            out.append("Trial phase not specified in this record")
        return out[:4]


class LLMReranker:
    name = "llm"

    def __init__(self, settings: Settings, client: OpenRouterClient | None = None) -> None:
        self._settings = settings
        self._client = client or OpenRouterClient(settings)
        self._fallback = DeterministicReranker()

    async def rerank(self, profile: PatientProfile, candidates: list[Candidate], top_k: int) -> list[TrialResult]:
        if not self._client.enabled or not candidates:
            return self._fallback.rerank(profile, candidates, top_k)
        try:
            return await self._llm_rerank(profile, candidates, top_k)
        except (LLMUnavailable, Exception):
            return self._fallback.rerank(profile, candidates, top_k)

    async def _llm_rerank(self, profile, candidates, top_k) -> list[TrialResult]:
        # Start from the deterministic result (guarantees schema + a floor of quality),
        # then ask the LLM to enrich reasons/cautions and adjust confidence for the
        # top slice. This bounds cost and keeps a safe fallback shape.
        base = self._fallback.rerank(profile, candidates, top_k)
        slice_n = min(len(base), 12)
        trials_json = [
            {
                "nct": r.nct, "title": r.title, "summary": r.brief_summary,
                "conditions": r.conditions, "interventions": r.interventions,
                "status": r.status, "phase": r.phase,
                "base_confidence": r.match_score, "contraindications": r.contraindications,
            }
            for r in base[:slice_n]
        ]
        system = (
            "You are a clinical trial-matching reasoning assistant. Given a "
            "de-identified patient profile and candidate trials, produce concise, "
            "clinically-grounded reasons and cautions per trial, and a calibrated "
            "confidence 0-100 (FIT, not eligibility). Separate reasons (why it fits) "
            "from cautions (what to verify). Honor contraindications: never raise "
            "confidence for a trial that conflicts with the patient's biomarker "
            "direction. Return JSON: {\"trials\":[{\"nct\":str,\"confidence\":number,"
            "\"reasons\":[str],\"cautions\":[str]}]}."
        )
        user = (
            f"PATIENT (de-identified):\n{profile.model_dump_json()}\n\n"
            f"CANDIDATE TRIALS:\n{trials_json}\n\nReturn the JSON."
        )
        raw = await self._client.complete_json(
            model=self._settings.llm_rerank_model, system=system, user=user,
            max_tokens=3000, temperature=0.1,
        )
        enrich = {t.get("nct"): t for t in raw.get("trials", []) if isinstance(t, dict)}
        for r in base[:slice_n]:
            e = enrich.get(r.nct)
            if not e:
                continue
            if e.get("reasons"):
                r.reasons = [str(x) for x in e["reasons"]][:5]
            if e.get("cautions"):
                r.cautions = [str(x) for x in e["cautions"]][:5]
            if isinstance(e.get("confidence"), (int, float)) and not r.contraindications:
                r.match_score = max(0.0, min(100.0, float(e["confidence"])))
                r.fit_label = _fit_label(r.match_score, bool(r.contraindications))
            r.explained_by = "llm"
        base[:slice_n] = sorted(base[:slice_n], key=lambda r: r.match_score, reverse=True)
        for i, r in enumerate(base, start=1):
            r.rank = i
        return base
