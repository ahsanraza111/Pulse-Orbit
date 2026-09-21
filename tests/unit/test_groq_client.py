from types import SimpleNamespace

import pytest

from pulse.application.errors import ProviderError
from pulse.application.models import ChatMessage
from pulse.infrastructure.llm.groq_client import GroqLLMClient


class FakeCompletions:
    def __init__(self, content: str | None = "answer", error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


def fake_client(completions: FakeCompletions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


@pytest.mark.asyncio
async def test_groq_adapter_maps_messages_and_options() -> None:
    completions = FakeCompletions("  answer  ")
    adapter = GroqLLMClient(
        fake_client(completions),
        model="model-name",
        temperature=0.2,
        max_completion_tokens=100,
    )

    result = await adapter.complete([ChatMessage(role="user", content="question")])

    assert result == "answer"
    assert completions.kwargs == {
        "model": "model-name",
        "messages": [{"role": "user", "content": "question"}],
        "temperature": 0.2,
        "max_completion_tokens": 100,
    }


@pytest.mark.asyncio
async def test_groq_adapter_wraps_provider_errors() -> None:
    adapter = GroqLLMClient(
        fake_client(FakeCompletions(error=TimeoutError())),
        model="model",
        temperature=0,
        max_completion_tokens=100,
    )

    with pytest.raises(ProviderError, match="temporarily unavailable"):
        await adapter.complete([ChatMessage(role="user", content="question")])


@pytest.mark.asyncio
async def test_groq_adapter_rejects_empty_response() -> None:
    adapter = GroqLLMClient(
        fake_client(FakeCompletions(content="  ")),
        model="model",
        temperature=0,
        max_completion_tokens=100,
    )

    with pytest.raises(ProviderError, match="empty response"):
        await adapter.complete([ChatMessage(role="user", content="question")])

