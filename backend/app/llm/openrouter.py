"""OpenRouter client — the single egress point for chart-derived data.

Privacy posture:
  * Only DE-IDENTIFIED text is ever passed here (enforced by the pipeline, not this
    module — but this is the one place data leaves the machine, so it is kept small
    and auditable).
  * Zero-Data-Retention routing is requested by default, so OpenRouter only routes
    to providers that retain nothing.
  * No prompt or response is logged.

Reliability:
  * Structured-JSON requests use response_format json_object and are validated by
    the caller against a pydantic schema (retry on parse failure).
  * Bounded retries with backoff on transient errors. A hard failure raises
    LLMUnavailable, which callers translate into graceful degradation.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx

from app.config import Settings


class LLMUnavailable(RuntimeError):
    """Raised when the LLM cannot be reached or returns no usable content.
    Callers degrade gracefully (e.g. fall back to rules extraction)."""


@dataclass
class LLMResponse:
    content: str
    model: str


class OpenRouterClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.openrouter_base_url.rstrip("/")
        self._timeout = settings.llm_timeout_seconds

    @property
    def enabled(self) -> bool:
        return self._settings.llm_enabled

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.openrouter_api_key}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers (optional, no PHI):
            "HTTP-Referer": "https://localhost/find-my-trial",
            "X-Title": "Find My Trial",
        }

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        max_retries: int = 3,
    ) -> dict:
        """Request a JSON object completion. Returns the parsed dict.
        Raises LLMUnavailable on unrecoverable failure."""
        if not self.enabled:
            raise LLMUnavailable("OpenRouter API key not configured")

        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if self._settings.openrouter_enforce_zdr:
            # Restrict routing to zero-data-retention providers.
            payload["provider"] = {"zdr": True}

        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        f"{self._base_url}/chat/completions",
                        headers=self._headers(),
                        json=payload,
                    )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError(
                        f"transient {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return json.loads(content)
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_err = exc
                await asyncio.sleep(min(2 ** attempt, 8) * 0.5)
            except (json.JSONDecodeError, KeyError, IndexError) as exc:
                # Malformed structured output — retry once or twice, then give up.
                last_err = exc
                await asyncio.sleep(0.3)

        raise LLMUnavailable(f"OpenRouter request failed after {max_retries} attempts: {last_err}")
