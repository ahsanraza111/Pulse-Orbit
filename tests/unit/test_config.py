import pytest
from pydantic import SecretStr

from pulse.core.config import Settings


def test_runtime_validation_accepts_credentials() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        teams_client_id="client",
        teams_client_secret=SecretStr("secret"),
        teams_tenant_id="tenant",
        groq_api_key=SecretStr("groq"),
    )

    settings.validate_runtime()


def test_runtime_validation_lists_missing_providers() -> None:
    with pytest.raises(ValueError) as error:
        Settings(
            _env_file=None,
            app_env="test",
            teams_client_id=None,
            teams_client_secret=None,
            teams_tenant_id=None,
            groq_api_key=None,
        ).validate_runtime()

    message = str(error.value)
    assert "Teams credentials" in message
    assert "Groq API key" in message


def test_skip_auth_is_rejected_outside_development() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        teams_skip_auth=True,
        groq_api_key=SecretStr("groq"),
    )

    with pytest.raises(ValueError, match="only be skipped in development"):
        settings.validate_runtime()
