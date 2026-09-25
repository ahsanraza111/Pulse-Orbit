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

    orbit_supabase_url: str | None = None
    orbit_supabase_anon_key: SecretStr | None = None
    orbit_session_encryption_key: SecretStr | None = None
    orbit_http_timeout_seconds: float = Field(default=15, gt=0, le=120)
    orbit_confirmation_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    orbit_business_timezone: str = "Asia/Karachi"
    orbit_max_duration_minutes: int = Field(default=1440, ge=1, le=10080)
    orbit_max_notes_chars: int = Field(default=2000, ge=1, le=10000)
    orbit_view_page_size: int = Field(default=5, ge=1, le=10)
    orbit_view_max_range_days: int = Field(default=366, ge=1, le=3660)
    orbit_session_ttl_minutes: int = Field(default=60, ge=5, le=1440)

    database_host: str | None = None
    database_port: int = Field(default=5432, ge=1, le=65535)
    database_name: str | None = None
    database_user: str | None = None
    database_password: SecretStr | None = None

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

    @property
    def orbit_is_configured(self) -> bool:
        return self.orbit_provider_is_configured and self.database_is_configured

    @property
    def orbit_provider_is_configured(self) -> bool:
        return bool(
            self.orbit_supabase_url
            and self.orbit_supabase_anon_key
            and self.orbit_session_encryption_key
        )

    @property
    def database_is_configured(self) -> bool:
        return bool(
            self.database_host
            and self.database_name
            and self.database_user
            and self.database_password
        )

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
        orbit_values = (
            self.orbit_supabase_url,
            self.orbit_supabase_anon_key,
            self.orbit_session_encryption_key,
        )
        if (
            any(value is not None for value in orbit_values)
            and not self.orbit_provider_is_configured
        ):
            errors.append(
                "Orbit URL, anon key, and session encryption key must be configured together"
            )
        database_values = (
            self.database_host,
            self.database_name,
            self.database_user,
            self.database_password,
        )
        if (
            any(value is not None for value in database_values)
            and not self.database_is_configured
        ):
            errors.append("Database host, name, user, and password must be configured together")
        if self.orbit_provider_is_configured and not self.database_is_configured:
            errors.append("PostgreSQL configuration is required when Orbit is enabled")
        if errors:
            raise ValueError("Invalid PULSE configuration: " + "; ".join(errors))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
