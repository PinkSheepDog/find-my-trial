"""LLM-backed patient extractor (OpenRouter, model-agnostic).

Runs on DE-IDENTIFIED text only. Produces the same PatientProfile schema as the
rules extractor. The prompt is engineered around the one property that matters
most: biomarker DIRECTION must be explicit and correct (HER2-low and any
"negative" finding must never be labeled positive).

On any LLM failure or schema-validation failure, the caller falls back to the
deterministic rules extractor, so the system always produces a profile.
"""

from __future__ import annotations

from pydantic import ValidationError

from app.config import Settings
from app.extraction.rules_extractor import RulesExtractor
from app.extraction.schema import PatientProfile
from app.llm.openrouter import LLMUnavailable, OpenRouterClient

_SYSTEM = """You are a clinical information extraction engine for an oncology \
trial-matching tool. You receive DE-IDENTIFIED chart text (identifiers already \
replaced with tags like [NAME], [DATE]). Extract a structured patient profile.

You are decision-support, not an eligibility determiner. Extract only what the \
chart supports; do not infer treatments or biomarkers that are not stated.

CRITICAL RULES:
- Every biomarker MUST include a `status` of exactly one of: \
"positive", "negative", "low", "equivocal", "unknown".
- "HER2 IHC 1+", "HER2-low", or "FISH not amplified" => status "low" (NOT positive).
- "HER2 IHC 3+" or "HER2-positive" or "amplified" => status "positive".
- "BRCA negative", "MSI stable", "wild type", "not detected" => status "negative".
- Never record a biomarker as "positive" unless the chart explicitly states a \
positive/amplified/mutated/high finding.
- For the messy chart, prefer the MOST RECENT finding when a marker is mentioned \
multiple times, and mark `certainty` "uncertain" if the chart conflicts.
- Capture prior-therapy toxicities (e.g. "grade 3 immune-mediated hepatitis") in \
the therapy's `caused_toxicity` field — these drive safety cautions.

Return ONLY a JSON object matching this shape:
{
  "age": int|null, "sex": "Female"|"Male"|null,
  "diagnosis": str|null, "cancer_types": [str],
  "stage": str|null, "is_metastatic": bool, "disease_sites": [str],
  "biomarkers": [{"name": str, "status": str, "detail": str|null, "certainty": "stated"|"inferred"|"uncertain"}],
  "therapies": [{"name": str, "is_current": bool, "caused_toxicity": str|null}],
  "ecog": int|null, "comorbidities": [str], "organ_function_flags": [str],
  "location_preferences": [str],
  "evidence": [{"field": str, "snippet": str}],
  "missing_or_uncertain": [str]
}
"""


class LLMExtractor:
    name = "llm"

    def __init__(self, settings: Settings, client: OpenRouterClient | None = None) -> None:
        self._settings = settings
        self._client = client or OpenRouterClient(settings)
        self._fallback = RulesExtractor()

    async def extract(self, deidentified_text: str) -> PatientProfile:
        if not self._client.enabled:
            return self._fallback.extract(deidentified_text)
        try:
            raw = await self._client.complete_json(
                model=self._settings.llm_extraction_model,
                system=_SYSTEM,
                user=f"CHART:\n{deidentified_text}\n\nReturn the JSON profile.",
                max_tokens=2048,
                temperature=0.0,
            )
            profile = PatientProfile.model_validate({**raw, "extractor": "llm"})
            # Safety net: re-assert direction guarantees even on a well-formed LLM result.
            self._enforce_direction_invariants(profile, deidentified_text)
            return profile
        except (LLMUnavailable, ValidationError, TypeError):
            # Deterministic degradation — never fail to produce a profile.
            return self._fallback.extract(deidentified_text)

    @staticmethod
    def _enforce_direction_invariants(profile: PatientProfile, text: str) -> None:
        """Defense-in-depth: even if the LLM ignored instructions, a HER2 marked
        positive while the chart clearly says 'IHC 1+'/'not amplified'/'her2-low'
        is downgraded to low. We never silently trust positive HER2 against explicit
        low/negative evidence."""
        lower = text.lower()
        her2 = profile.biomarker("HER2")
        if her2 and her2.status.value == "positive":
            if any(cue in lower for cue in ("ihc 1+", "her2-low", "her2 low", "not amplified")):
                from app.extraction.schema import BiomarkerStatus, Certainty
                her2.status = BiomarkerStatus.LOW
                her2.certainty = Certainty.UNCERTAIN
