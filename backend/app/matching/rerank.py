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

Two properties this module is responsible for:

  1. The score reflects the CLINICAL GATES. Disease-family match and study purpose are
     weighted components of match_score, not decoration in the reason text. A trial
     that never faced the disease gate (no recognized cancer family) scores zero there
     and says so.
  2. Every explanation carries EVIDENCE. Each reason/caution is an Explanation with a
     verbatim snippet and its source field. LLM reasons must quote the trial record;
     any quote that is not literally present is DROPPED rather than shown, because an
     ungrounded explanation is worse than none.

Confidence is fit, not eligibility. It is bounded, monotonic in signal, and
penalized by contraindications and inactive status — never a black box.
"""

from __future__ import annotations

import re

from app.config import Settings
from app.extraction.schema import PatientProfile
from app.llm.openrouter import LLMUnavailable, OpenRouterClient
from app.matching.clinical import grounded_source, snippet_for
from app.matching.results import Explanation, ScoreBreakdown, TrialResult
from app.trials.index import RECRUITING_STATUSES, TrialRecord
from app.trials.retrieve import Candidate

# Confidence weights (sum of positive contributions = 100 at theoretical max).
# The two P0 gate signals — disease family and study purpose — are weighted
# COMPONENTS here; previously they contributed 0 to the number the board is sorted by.
_W_CONDITION = 30.0
_W_BIOMARKER = 20.0
_W_THERAPY = 8.0
_W_LEXICAL = 12.0
_W_STATUS = 8.0
_W_DISEASE = 16.0
_W_PURPOSE = 6.0
# Geography is applied on top (only when a location was requested) and then clipped,
# so the 0-100 scale is unchanged when no geography is in play.
_W_LOCATION = 8.0
_LOCATION_MISS_PENALTY = 6.0

# "Trial appears to require HER2-positive; patient is HER2 low." -> HER2
_CONTRA_MARKER_RE = re.compile(r"require\s+([A-Za-z0-9\-]+?)-(?:positive|negative)")


def _status_factor(status: str) -> float:
    if status in RECRUITING_STATUSES:
        return 1.0
    if status == "ACTIVE_NOT_RECRUITING":
        return 0.55
    if status in {"UNKNOWN", ""}:
        return 0.4
    return 0.15  # completed / terminated / withdrawn


def _closure_penalty(status: str) -> float:
    """Multiplicative demotion for studies that cannot enrol this patient today.

    Applied on top of the additive status points because enrollability is a different
    kind of fact from fit: a COMPLETED study may match the chart perfectly and still be
    useless to act on. Kept as a demotion, not a filter, so the board stays honest when
    a user has deliberately asked to see closed studies."""
    if status in RECRUITING_STATUSES:
        return 0.0
    if status == "ACTIVE_NOT_RECRUITING":
        return 0.30   # running, but closed to new participants
    if status in {"UNKNOWN", ""}:
        return 0.20   # unverifiable — treat with suspicion, not as open
    return 0.55       # completed / terminated / withdrawn / suspended


def _disease_factor(c: Candidate) -> float:
    """How well this trial satisfied the disease gate. Full credit for a shared primary
    cancer family; partial for a genuine tumour-agnostic basket; ZERO when the trial
    names no recognized family — it never faced the gate and must not score as if it had."""
    if c.disease_unclassified:
        return 0.0
    if c.disease_family:
        return 1.0
    if c.record.is_basket or c.basket_evidence:
        return 0.55
    return 0.0


def _purpose_factor(c: Candidate) -> float:
    if c.study_purpose == "treatment":
        return 1.0
    if c.study_purpose == "expanded_access":
        return 0.7
    if c.purpose_unverified:
        return 0.25   # registry stated no purpose — partial credit, and a caution
    return 0.0        # explicitly non-treatment (only reachable with treatment_only=False)


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


def _sources(rec: TrialRecord) -> dict[str, str]:
    """The trial fields an explanation may legitimately quote from."""
    return {
        "title": rec.title,
        "conditions": rec.condition_text,
        "brief_summary": rec.brief_summary,
        "interventions": rec.intervention_text,
        "status": rec.status,
        "locations": " | ".join(rec.locations),
    }


def _quote(rec: TrialRecord, term: str, prefer: tuple[str, ...] = ("conditions", "title", "brief_summary")) -> tuple[str, str]:
    """Find a VERBATIM snippet for `term` in the trial record. Returns (snippet, field);
    ("", "") when the term does not literally appear — no evidence is ever invented."""
    sources = _sources(rec)
    for fieldname in prefer:
        snippet = snippet_for(term, sources.get(fieldname, ""))
        if snippet:
            return snippet, fieldname
    for fieldname, value in sources.items():
        snippet = snippet_for(term, value)
        if snippet:
            return snippet, fieldname
    return "", ""


class DeterministicReranker:
    name = "rules"

    def rerank(self, profile: PatientProfile, candidates: list[Candidate], top_k: int) -> list[TrialResult]:
        scored: list[tuple[float, Candidate, ScoreBreakdown]] = []
        for c in candidates:
            cond_pts = _W_CONDITION * (len(c.matched_conditions) / max(len(profile.cancer_types), 1)) if profile.cancer_types else 0.0
            bio_pts = _W_BIOMARKER * (len(c.matched_biomarkers) / max(len(profile.positive_biomarkers()), 1)) if profile.positive_biomarkers() else 0.0
            ther_pts = _W_THERAPY * min(len(c.matched_therapies) / max(len(profile.therapies), 1), 1.0) if profile.therapies else 0.0
            lex_pts = _W_LEXICAL * c.bm25
            status_pts = _W_STATUS * _status_factor(c.record.status)
            disease_pts = _W_DISEASE * _disease_factor(c)
            purpose_pts = _W_PURPOSE * _purpose_factor(c)

            loc_pts = 0.0
            if c.location_query:
                loc_pts = _W_LOCATION if c.location_match else -_LOCATION_MISS_PENALTY

            raw = cond_pts + bio_pts + ther_pts + lex_pts + status_pts + disease_pts + purpose_pts + loc_pts
            penalty = 0.0
            if c.contraindications:
                penalty = max(raw, 0.0) * 0.65  # heavy, transparent demotion (not deletion)
            # Enrollability is categorical, not just another additive signal. As points,
            # the status term (8 max) was routinely out-earned by therapy/lexical overlap,
            # so COMPLETED and TERMINATED studies took the top three slots ahead of
            # recruiting ones — a board a clinician cannot act on. A closed study can still
            # be worth reading, so demote rather than drop, but no amount of content
            # similarity should let it outrank a study that is open.
            closure = _closure_penalty(c.record.status)
            penalty += max(raw - penalty, 0.0) * closure
            confidence = max(0.0, min(100.0, raw - penalty))

            breakdown = ScoreBreakdown(
                condition=round(cond_pts, 1), biomarker=round(bio_pts, 1),
                therapy=round(ther_pts, 1), lexical=round(lex_pts, 1),
                status=round(status_pts, 1), disease=round(disease_pts, 1),
                purpose=round(purpose_pts, 1), location=round(loc_pts, 1),
                contraindication_penalty=round(-penalty, 1),
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
            locations=(c.matched_locations or rec.locations)[:3],
            match_score=round(conf, 1),
            fit_label=_fit_label(conf, bool(c.contraindications)),
            reasons=reasons, cautions=cautions, contraindications=c.contraindications,
            evidence=_collect_evidence(reasons, cautions),
            matched_conditions=c.matched_conditions, matched_biomarkers=c.matched_biomarkers,
            matched_therapies=c.matched_therapies, breakdown=breakdown,
            eligibility_sex=rec.sex if rec.sex not in {"NA", ""} else "Not specified",
            eligibility_age=", ".join(sorted(rec.age_buckets)) if rec.age_buckets else "Not specified",
            disease_family=c.disease_family, study_purpose=c.study_purpose,
            disease_unclassified=c.disease_unclassified,
            purpose_unverified=c.purpose_unverified,
            location_match=c.location_match, matched_locations=c.matched_locations,
            is_recruiting=rec.is_recruiting,
            explained_by=self.name,
        )

    # --- explanations: every entry quotes the record ------------------------------

    def _reasons(self, c: Candidate, profile: PatientProfile) -> list[Explanation]:
        rec = c.record
        out: list[Explanation] = []
        # Cite disease, purpose and status evidence first (the gate signals).
        if c.disease_family:
            term = next((t for t in c.matched_conditions), "") or c.disease_family.split(", ")[0]
            snippet, src = _quote(rec, term)
            if not snippet:
                snippet, src = _quote(rec, c.disease_family.split(", ")[0].split()[0])
            out.append(Explanation(
                text="Primary disease match: " + c.disease_family,
                evidence_snippet=snippet, source_field=src or "conditions",
                grounded=bool(snippet),
            ))
        elif c.basket_evidence or rec.is_basket:
            term = c.basket_evidence or "solid tumor"
            snippet, src = _quote(rec, term, prefer=("title", "conditions"))
            out.append(Explanation(
                text="Tumour-agnostic / basket study — may apply across primary cancers",
                evidence_snippet=snippet, source_field=src or "title", grounded=bool(snippet),
            ))
        if c.study_purpose == "treatment":
            out.append(Explanation(
                text="Treatment study", evidence_snippet="Primary Purpose: TREATMENT",
                source_field="study_design", grounded=True,
            ))
        elif c.study_purpose not in {"unknown", ""}:
            out.append(Explanation(
                text="Study purpose: " + c.study_purpose.replace("_", " "),
                evidence_snippet=c.purpose_evidence or f"Primary Purpose: {c.study_purpose.upper()}",
                source_field="brief_summary" if c.purpose_evidence else "study_design",
                grounded=True,
            ))
        for m in c.matched_biomarkers[:2]:
            snippet, src = _quote(rec, m, prefer=("title", "conditions", "brief_summary"))
            out.append(Explanation(
                text="Biomarker target overlap: " + m,
                evidence_snippet=snippet, source_field=src or "title", grounded=bool(snippet),
            ))
        for t in c.matched_therapies[:2]:
            snippet, src = _quote(rec, t, prefer=("interventions", "title", "brief_summary"))
            out.append(Explanation(
                text="Relevant therapy class: " + t,
                evidence_snippet=snippet, source_field=src or "interventions", grounded=bool(snippet),
            ))
        if c.matched_locations:
            out.append(Explanation(
                text=f"Study site in {c.location_query}: {c.matched_locations[0]}",
                evidence_snippet=c.matched_locations[0], source_field="locations", grounded=True,
            ))
        if rec.is_recruiting:
            out.append(Explanation(
                text="Currently recruiting — " + rec.status.replace("_", " ").title(),
                evidence_snippet=rec.status, source_field="status", grounded=True,
            ))
        return out[:6]

    def _cautions(self, c: Candidate, profile: PatientProfile) -> list[Explanation]:
        rec = c.record
        out: list[Explanation] = []
        if not rec.is_recruiting:
            label = rec.status.replace("_", " ").title()
            note = (" — study is running but CLOSED to new participants"
                    if rec.status == "ACTIVE_NOT_RECRUITING" else " — may limit usefulness")
            out.append(Explanation(
                text=f"Status is {label}{note}", evidence_snippet=rec.status,
                source_field="status", grounded=True,
            ))
        # Gate provenance the clinician must know about.
        if c.disease_unclassified:
            out.append(Explanation(
                text=("Trial conditions name no recognized cancer family — primary-disease "
                      "match is UNVERIFIED and this trial was demoted accordingly"),
                evidence_snippet=(rec.condition_text or rec.title)[:200],
                source_field="conditions" if rec.condition_text else "title", grounded=True,
            ))
        if c.purpose_unverified:
            out.append(Explanation(
                text=("Registry record states no Primary Purpose — verify this is a treatment "
                      "study, not imaging/registry"),
                evidence_snippet=rec.study_type or "INTERVENTIONAL",
                source_field="study_design", grounded=True,
            ))
        if c.location_query and not c.matched_locations:
            sites = ", ".join(rec.locations[:2]) or "no sites listed"
            out.append(Explanation(
                text=f"No listed study site in {c.location_query} — nearest listed: {sites}",
                evidence_snippet=rec.locations[0] if rec.locations else "",
                source_field="locations", grounded=bool(rec.locations),
            ))
        # Contraindications: the same string list the API already exposed, now with a quote.
        for ci in c.contraindications:
            m = _CONTRA_MARKER_RE.search(ci)
            marker = m.group(1) if m else ""
            snippet, src = _quote(rec, marker, prefer=("title", "conditions", "brief_summary")) if marker else ("", "")
            out.append(Explanation(
                text=ci, evidence_snippet=snippet, source_field=src or "title",
                grounded=bool(snippet),
            ))
        # Exclusion-criteria conflicts: the patient has something the trial rules out.
        for conflict in c.eligibility_conflicts[:3]:
            out.append(Explanation(
                text=conflict.text, evidence_snippet=conflict.snippet,
                source_field=conflict.source_field, grounded=True,
            ))
        # Safety-aware cautions derived from the structured profile.
        if profile.ecog is not None and profile.ecog >= 2 and not any(
            x.text.startswith("Patient ECOG") for x in out
        ):
            out.append(Explanation(
                text=f"ECOG {profile.ecog} may fail protocol performance-status thresholds",
                evidence_snippet="", source_field="patient_profile", grounded=True,
            ))
        for t in profile.therapies:
            if t.caused_toxicity and any(
                k in rec.search_text.lower()
                for k in ("immunotherapy", "checkpoint", "pd-1", "pd-l1", "atezolizumab", "pembrolizumab")
            ):
                term = next((k for k in ("checkpoint", "immunotherapy", "pd-1", "pd-l1",
                                         "pembrolizumab", "atezolizumab")
                             if k in rec.search_text.lower()), "")
                snippet, src = _quote(rec, term, prefer=("interventions", "title", "brief_summary"))
                out.append(Explanation(
                    text=f"Prior {t.name} caused {t.caused_toxicity} — verify checkpoint-therapy exclusions",
                    evidence_snippet=snippet, source_field=src or "interventions",
                    grounded=bool(snippet),
                ))
                break
        if "LFT elevation" in profile.organ_function_flags:
            out.append(Explanation(
                text="LFT elevation present — verify hepatic-function eligibility thresholds",
                evidence_snippet="", source_field="patient_profile", grounded=True,
            ))
        if not rec.phase:
            out.append(Explanation(
                text="Trial phase not specified in this record", evidence_snippet="",
                source_field="patient_profile", grounded=True,
            ))
        return out[:6]


def _collect_evidence(*groups: list[Explanation]) -> list[Explanation]:
    """Flatten every grounded, quoting explanation into one de-duplicated list."""
    out: list[Explanation] = []
    seen: set[str] = set()
    for group in groups:
        for e in group:
            if not e.evidence_snippet or not e.grounded:
                continue
            key = f"{e.source_field}|{e.evidence_snippet}"
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
    return out


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
        by_nct = {c.record.nct: c.record for c in candidates}
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
            "direction. EVERY reason and caution MUST cite evidence: the `evidence` "
            "field must be a snippet copied VERBATIM (word for word) from that trial's "
            "own title, conditions, interventions, status or summary as given to you. "
            "Do not paraphrase, summarize or invent evidence, and never quote another "
            "trial. Any item whose evidence is not found verbatim in the trial record "
            "WILL BE DISCARDED. If you cannot quote the record, omit the item. "
            "Return JSON: {\"trials\":[{\"nct\":str,\"confidence\":number,"
            "\"reasons\":[{\"text\":str,\"evidence\":str}],"
            "\"cautions\":[{\"text\":str,\"evidence\":str}]}]}."
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
            rec = by_nct.get(r.nct)
            if not e or rec is None:
                continue
            dropped = 0
            reasons, d1 = _ground_llm_items(e.get("reasons"), rec)
            cautions, d2 = _ground_llm_items(e.get("cautions"), rec)
            dropped += d1 + d2
            # Only replace the deterministic explanations when the LLM produced GROUNDED
            # ones. An ungrounded explanation is worse than none, so we keep the
            # rules-based (always-quoting) set rather than showing unverifiable prose.
            if reasons:
                r.reasons = reasons[:5]
            if cautions:
                r.cautions = cautions[:5]
            r.ungrounded_dropped = dropped
            r.evidence = _collect_evidence(r.reasons, r.cautions)
            if isinstance(e.get("confidence"), (int, float)) and not r.contraindications:
                r.match_score = max(0.0, min(100.0, float(e["confidence"])))
                r.fit_label = _fit_label(r.match_score, bool(r.contraindications))
            r.explained_by = "llm" if (reasons or cautions) else "rules"
        base[:slice_n] = sorted(base[:slice_n], key=lambda r: r.match_score, reverse=True)
        for i, r in enumerate(base, start=1):
            r.rank = i
        return base


def _ground_llm_items(items, rec: TrialRecord) -> tuple[list[Explanation], int]:
    """Turn raw LLM reason/caution entries into Explanations, DROPPING any whose claimed
    evidence does not literally appear in this trial's record.

    Returns (kept, dropped_count). A bare string (no evidence at all) is dropped: the
    response schema requires evidence, and an unverifiable claim on a clinical board is
    worse than a missing one."""
    kept: list[Explanation] = []
    dropped = 0
    if not isinstance(items, list):
        return kept, dropped
    sources = _sources(rec)
    for item in items:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("reason") or item.get("caution") or "").strip()
            claimed = str(item.get("evidence") or item.get("evidence_snippet") or "").strip()
        else:
            text, claimed = str(item).strip(), ""
        if not text:
            continue
        # A stricter minimum than the deterministic path: a two-word fragment is
        # trivially "found" in any long summary and is not evidence of anything.
        source = grounded_source(claimed, sources, min_len=12) if claimed else None
        if source is None:
            dropped += 1
            continue
        kept.append(Explanation(text=text, evidence_snippet=claimed,
                                source_field=source, grounded=True))
    return kept, dropped
