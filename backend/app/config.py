"""Centralized configuration. All secrets and tunables come from the environment
(via a gitignored .env file) — never hardcoded. Nothing in the codebase reads
os.environ directly; everything goes through the validated Settings object.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = .../backend
BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FMT_",
        env_file=str(BACKEND_DIR.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Secrets ---
    secret_key: str = Field(default="dev-insecure-key-change-me", min_length=8)
    openrouter_api_key: str = ""

    # --- LLM models (model-agnostic via OpenRouter) ---
    llm_extraction_model: str = "anthropic/claude-haiku-4.5"
    llm_rerank_model: str = "anthropic/claude-sonnet-4.6"
    openrouter_enforce_zdr: bool = True
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_timeout_seconds: float = 60.0

    # --- Data ---
    trials_csv_path: str = "data/trials.csv"

    # --- Auth / session ---
    session_idle_timeout_minutes: int = 30
    session_cookie_name: str = "fmt_session"
    admin_username: str = "admin"
    admin_password: str = ""

    # --- Intake / de-identification ---
    require_deid_review: bool = True
    use_presidio: bool = False
    max_upload_mb: int = 15

    # --- Runtime ---
    env: str = "development"
    cors_origins: str = "http://localhost:5173"

    @field_validator("trials_csv_path")
    @classmethod
    def _resolve_csv(cls, value: str) -> str:
        p = Path(value)
        if not p.is_absolute():
            p = BACKEND_DIR / value
        return str(p)

    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"production", "prod"}

    @property
    def llm_enabled(self) -> bool:
        """True when a real OpenRouter key is present. When False, the system runs
        in deterministic degraded mode (rule-based extraction, no LLM rerank)."""
        return bool(self.openrouter_api_key.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
