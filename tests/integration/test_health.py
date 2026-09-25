import pytest
from httpx import ASGITransport, AsyncClient

from pulse.core.config import Settings
from pulse.presentation.http import create_http_app


@pytest.mark.asyncio
async def test_liveness(http_client: AsyncClient) -> None:
    response = await http_client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readiness_does_not_expose_secrets(http_client: AsyncClient) -> None:
    response = await http_client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "teams_configured": True,
        "groq_configured": True,
        "orbit_configured": False,
        "database_configured": False,
        "database_connected": False,
    }
    assert "test-client-secret" not in response.text
    assert "test-groq-key" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_ready", "expected_status"),
    [(True, 200), (False, 503)],
)
async def test_readiness_checks_required_orbit_database(
    database_ready: bool,
    expected_status: int,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        teams_client_id="client",
        teams_client_secret="secret",
        teams_tenant_id="tenant",
        groq_api_key="groq",
        orbit_supabase_url="https://orbit.example",
        orbit_supabase_anon_key="public-key",
        orbit_session_encryption_key="fernet-key",
        database_host="127.0.0.1",
        database_name="pulse",
        database_user="postgres",
        database_password="database-secret",
    )

    async def probe() -> bool:
        return database_ready

    app = create_http_app(settings, probe)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == expected_status
    assert response.json()["database_connected"] is database_ready
