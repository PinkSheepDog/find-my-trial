"""End-to-end matching pipeline orchestration.

  de-identified text -> extract profile -> retrieve candidates -> rerank/explain

The pipeline NEVER receives raw chart text; it operates on already-de-identified
text (the API layer enforces de-id + human review before calling this). It holds
no patient state — each call is stateless and returns a value; nothing is persisted.
"""

from __future__ import annotations

from app.config import Settings
from app.extraction.llm_extractor import LLMExtractor
from app.extraction.rules_extractor import RulesExtractor
from app.extraction.schema import PatientProfile
from app.matching.rerank import DeterministicReranker, LLMReranker
from app.matching.results import MatchResponse
from app.trials.index import TrialIndex
from app.trials.retrieve import RetrievalFilters, retrieve


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
            return await self._llm_extractor.extract(deidentified_text)
        return self._rules_extractor.extract(deidentified_text)

    async def match(
        self,
        profile: PatientProfile,
        *,
        top_k: int = 10,
        filters: RetrievalFilters | None = None,
    ) -> MatchResponse:
        filters = filters or RetrievalFilters()
        candidates = retrieve(profile, self._index, filters=filters, top_k=max(top_k * 4, 40))

        if self._llm_enabled:
            results = await self._llm_reranker.rerank(profile, candidates, top_k)
        else:
            results = self._det_reranker.rerank(profile, candidates, top_k)

        stats = self._index.stats()
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
