"""Trial corpus: load, index, and retrieve candidate trials.

This is the cheap, LLM-free stage. Public ClinicalTrials.gov data is loaded once,
hard-filtered by structured eligibility (cancer type, sex, age bucket, status),
then BM25-ranked to a small candidate set that the LLM reranker explains. No
patient text is ever embedded or sent anywhere from this layer.
"""
