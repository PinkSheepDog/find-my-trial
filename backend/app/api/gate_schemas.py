"""Schemas for the de-identification approval gate.

Kept out of api/schemas.py so the egress-gate contract stays legible on its own.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ApproveDeidRequest(BaseModel):
    text: str = Field(min_length=1, description="The reviewed, de-identified text being approved for matching.")


class ApproveDeidResponse(BaseModel):
    approval_token: str = Field(description="Send as the X-Deid-Approval header on /api/match.")
    expires_in_minutes: int
    residual_redactions: int = Field(
        default=0,
        description="Identifiers still found in the submitted text. Non-zero means approval was refused.",
    )
