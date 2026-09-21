from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from pulse.core.config import Settings
from pulse.presentation.http import create_http_app


@pytest.fixture
def configured_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        teams_client_id="test-client-id",
        teams_client_secret="test-client-secret",
        teams_tenant_id="test-tenant-id",
        groq_api_key="test-groq-key",
    )


@pytest.fixture
async def http_client(configured_settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_http_app(configured_settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
