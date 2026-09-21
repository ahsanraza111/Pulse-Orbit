from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PULSE_",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = Field(default=3978, ge=1, le=65535)

    teams_client_id: str | None = None
    teams_client_secret: SecretStr | None = None
    teams_tenant_id: str | None = None
    teams_skip_auth: bool = False

    groq_api_key: SecretStr | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    groq_temperature: float = Field(default=0.2, ge=0, le=2)
    groq_max_completion_tokens: int = Field(default=1024, ge=1, le=8192)
    groq_timeout_seconds: float = Field(default=30, gt=0, le=120)

    max_user_message_chars: int = Field(default=8000, ge=1, le=50000)
    system_prompt: str = (
        "You are PULSE, a concise and helpful internal assistant. Never claim that a "
        "timesheet operation succeeded unless the Orbit service confirms it."
    )

    @property
    def teams_is_configured(self) -> bool:
        return bool(
            self.teams_skip_auth
            or (self.teams_client_id and self.teams_client_secret and self.teams_tenant_id)
        )

    @property
    def groq_is_configured(self) -> bool:
        return self.groq_api_key is not None

    def validate_runtime(self) -> None:
        errors: list[str] = []
        if not self.teams_is_configured:
            errors.append(
                "Teams credentials are required (client ID, client secret, and tenant ID)"
            )
        if self.teams_skip_auth and self.app_env != "development":
            errors.append("Teams authentication can only be skipped in development")
        if not self.groq_is_configured:
            errors.append("Groq API key is required")
        if errors:
            raise ValueError("Invalid PULSE configuration: " + "; ".join(errors))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

