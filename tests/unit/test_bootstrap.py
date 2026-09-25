import pytest
from cryptography.fernet import Fernet

from pulse.bootstrap import Container
from pulse.core.config import Settings


@pytest.mark.asyncio
async def test_container_wires_optional_orbit_services() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        teams_client_id="teams-client",
        teams_client_secret="teams-secret",
        teams_tenant_id="teams-tenant",
        groq_api_key="groq-key",
        orbit_supabase_url="https://orbit.example",
        orbit_supabase_anon_key="publishable-key",
        orbit_session_encryption_key=Fernet.generate_key().decode(),
    )

    container = Container.build(settings)
    try:
        assert container.orbit_auth_service is not None
        assert container.orbit_add_service is not None
        assert container.orbit_http_client is not None
    finally:
        await container.close()
