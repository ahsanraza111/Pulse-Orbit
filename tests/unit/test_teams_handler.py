from types import SimpleNamespace

import pytest

from pulse.application.errors import ProviderError
from pulse.presentation.teams import TeamsMessageHandler, clean_teams_text


class FakeChatService:
    def __init__(self, response: str = "reply", error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.received = None

    async def reply(self, text: str) -> str:
        self.received = text
        if self.error:
            raise self.error
        return self.response


class FakeContext:
    def __init__(self, text: str | None) -> None:
        self.activity = SimpleNamespace(text=text)
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


def test_clean_teams_text_removes_mentions() -> None:
    assert clean_teams_text("<at>PULSE</at>  hello") == "hello"


@pytest.mark.asyncio
async def test_handler_replies_in_context() -> None:
    service = FakeChatService()
    context = FakeContext("<at>PULSE</at> hello")

    await TeamsMessageHandler(service)(context)

    assert service.received == "hello"
    assert context.sent == ["reply"]


@pytest.mark.asyncio
async def test_handler_returns_safe_provider_error() -> None:
    service = FakeChatService(error=ProviderError("secret provider detail"))
    context = FakeContext("hello")

    await TeamsMessageHandler(service)(context)

    assert context.sent == [
        "PULSE cannot reach the AI service right now. Please try again shortly."
    ]

