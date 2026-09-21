from collections.abc import Sequence

import pytest

from pulse.application.errors import InvalidMessageError
from pulse.application.models import ChatMessage
from pulse.application.services.chat import ChatService


class FakeLLMClient:
    def __init__(self, response: str = "hello from PULSE") -> None:
        self.response = response
        self.messages: Sequence[ChatMessage] = ()

    async def complete(self, messages: Sequence[ChatMessage]) -> str:
        self.messages = messages
        return self.response


@pytest.mark.asyncio
async def test_chat_service_builds_controlled_prompt() -> None:
    llm = FakeLLMClient()
    service = ChatService(llm_client=llm, system_prompt="system rule", max_user_message_chars=20)

    response = await service.reply("  hello  ")

    assert response == "hello from PULSE"
    assert llm.messages == (
        ChatMessage(role="system", content="system rule"),
        ChatMessage(role="user", content="hello"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   "])
async def test_chat_service_rejects_empty_input(text: str) -> None:
    service = ChatService(FakeLLMClient(), "system", 20)

    with pytest.raises(InvalidMessageError, match="text message"):
        await service.reply(text)


@pytest.mark.asyncio
async def test_chat_service_rejects_oversized_input() -> None:
    service = ChatService(FakeLLMClient(), "system", 3)

    with pytest.raises(InvalidMessageError, match="too long"):
        await service.reply("four")

