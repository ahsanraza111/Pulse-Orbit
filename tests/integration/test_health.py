import pytest
from httpx import AsyncClient


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
    }
    assert "test-client-secret" not in response.text
    assert "test-groq-key" not in response.text

