"""Document intake: text extraction, OCR, and de-identification.

Pipeline order is a HARD invariant:  extract text -> de-identify -> (human review) -> LLM.
No raw chart text ever leaves the machine un-de-identified.
"""
