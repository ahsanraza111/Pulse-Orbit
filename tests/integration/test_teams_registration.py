from collections.abc import Sequence

import pytest

from pulse.application.models import ChatMessage
from pulse.application.services.chat import ChatService
from pulse.core.config import Settings
from pulse.presentation.http import create_http_app
from pulse.presentation.teams import register_teams_app


class NoOpLLMClient:
    async def complete(self, messages: Sequence[ChatMessage]) -> str:
        return "ok"


@pytest.mark.asyncio
async def test_teams_sdk_registers_messages_endpoint() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        teams_client_id="test-client-id",
        teams_client_secret="test-client-secret",
        teams_tenant_id="test-tenant-id",
        groq_api_key="test-groq-key",
    )
    http_app = create_http_app(settings)
    chat_service = ChatService(NoOpLLMClient(), "system", 100)
    teams_app = register_teams_app(
        http_app,
        settings,
        chat_service,
        orbit_auth_service=object(),  # type: ignore[arg-type]
        orbit_add_service=object(),  # type: ignore[arg-type]
    )

    await teams_app.initialize()

    messages_routes = [
        route
        for route in http_app.routes
        if getattr(route, "path", None) == "/api/messages"
    ]
    assert len(messages_routes) == 1
    assert "POST" in messages_routes[0].methods
