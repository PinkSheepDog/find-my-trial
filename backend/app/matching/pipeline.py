"""End-to-end matching pipeline orchestration.

  de-identified text -> extract profile -> retrieve candidates -> rerank/explain

The pipeline NEVER receives raw chart text; it operates on already-de-identified
text (the API layer enforces de-id + human review before calling this). It holds
no patient state — each call is stateless and returns a value; nothing is persisted.
"""

from __future__ import annotations

from app.config import Settings
from app.extraction.llm_extractor import LLMExtractor
from app.extraction.schema import BiomarkerStatus, PatientProfile, derive_facts
from app.extraction.rules_extractor import RulesExtractor
from app.matching.rerank import DeterministicReranker, LLMReranker
from app.matching.results import MatchResponse
from app.trials.index import TrialIndex
from app.trials.retrieve import RetrievalFilters, patient_disease_families, retrieve


class MatchingPipeline:
    def __init__(self, settings: Settings, index: TrialIndex) -> None:
        self._settings = settings
        self._index = index
        self._llm_enabled = settings.llm_enabled
        self._llm_extractor = LLMExtractor(settings)
        self._rules_extractor = RulesExtractor()
        self._llm_reranker = LLMReranker(settings)
        self._det_reranker = DeterministicReranker()

    async def extract_profile(self, deidentified_text: str) -> PatientProfile:
        if self._llm_enabled:
            profile = await self._llm_extractor.extract(deidentified_text)
        else:
            profile = self._rules_extractor.extract(deidentified_text)
        profile.facts = derive_facts(profile)   # reviewable, source-linked fact list
        return profile

    async def match(
        self,
        profile: PatientProfile,
        *,
        top_k: int = 10,
        filters: RetrievalFilters | None = None,
    ) -> MatchResponse:
        filters = filters or RetrievalFilters()
        stats = self._index.stats()

        # Abstention gate: if the profile is too thin, return NEEDS-REVIEW instead of a
        # confident ranked list (Case 04). Decision support must not over-claim.
        needs_review, review_reasons = _abstention(profile)
        if needs_review:
            return MatchResponse(
                results=[], candidate_count=0, trial_count=stats["trial_count"],
                semantic_used=False, degraded_mode=not self._llm_enabled,
                fallback_hint="Insufficient verified data to rank trials — resolve the items below and re-run.",
                needs_review=True, review_reasons=review_reasons,
            )

        candidates = retrieve(profile, self._index, filters=filters, top_k=max(top_k * 4, 40))

        if self._llm_enabled:
            results = await self._llm_reranker.rerank(profile, candidates, top_k)
        else:
            results = self._det_reranker.rerank(profile, candidates, top_k)

        return MatchResponse(
            results=results,
            candidate_count=len(candidates),
            trial_count=stats["trial_count"],
            semantic_used=self._llm_enabled,
            degraded_mode=not self._llm_enabled,
            fallback_hint=self._fallback_hint(results, filters),
        )

    async def run(
        self,
        deidentified_text: str,
        *,
        top_k: int = 10,
        filters: RetrievalFilters | None = None,
    ) -> tuple[PatientProfile, MatchResponse]:
        profile = await self.extract_profile(deidentified_text)
        response = await self.match(profile, top_k=top_k, filters=filters)
        return profile, response

    @staticmethod
    def _fallback_hint(results, filters: RetrievalFilters) -> str | None:
        if results:
            return None
        if filters.active_only and filters.interventional_only:
            return "No active interventional trials cleared the filters. Try relaxing 'recruiting only' or 'interventional only'."
        return "No trials cleared the current filters. Try broader chart text."


# --- Abstention ---------------------------------------------------------------
# A confident ranked list is only justified when a minimum set of core facts is
# present. Below the threshold we surface WHAT is missing and abstain.
_MIN_CORE_FACTS = 3


def _abstention(profile: PatientProfile) -> tuple[bool, list[str]]:
    """Return (needs_review, reasons). Needs review when fewer than _MIN_CORE_FACTS of
    the core matching facts (disease family, stage/extent, performance status, verified
    therapy, a definitive biomarker) are present."""
    present = 0
    missing: list[str] = []

    if patient_disease_families(profile):
        present += 1
    else:
        missing.append("Primary cancer / disease family")

    if profile.stage or profile.is_metastatic:
        present += 1
    else:
        missing.append("Stage / metastatic extent")

    if profile.ecog is not None:
        present += 1
    else:
        missing.append("Performance status (ECOG/KPS)")

    if profile.therapies:
        present += 1
    else:
        missing.append("Verified treatment history")

    definitive = {BiomarkerStatus.POSITIVE, BiomarkerStatus.NEGATIVE, BiomarkerStatus.LOW}
    if any(b.status in definitive for b in profile.biomarkers):
        present += 1
    else:
        missing.append("A resolved biomarker result (pending/missing does not count)")

    return (present < _MIN_CORE_FACTS, missing)
