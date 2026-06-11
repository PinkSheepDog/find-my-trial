"""Patient signal extraction: messy chart text -> structured PatientProfile.

Two interchangeable extractors implement the same contract:
  * RulesExtractor  — deterministic, negation-aware, offline, always available.
  * LLMExtractor    — OpenRouter-backed, higher recall on messy text.

Both MUST encode biomarker DIRECTION (positive/negative/low). The previous
prototype's fatal bug was treating "BRCA negative" and "HER2 IHC 1+" as positive;
the shared schema makes that representationally impossible.
"""
