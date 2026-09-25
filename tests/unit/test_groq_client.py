from types import SimpleNamespace

import httpx
import pytest
from groq import BadRequestError

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
        "tool_choice": "none",
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


@pytest.mark.asyncio
async def test_groq_adapter_retries_unexpected_tool_call_once() -> None:
    class RetryCompletions:
        def __init__(self) -> None:
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                response = httpx.Response(
                    400,
                    request=httpx.Request("POST", "https://api.groq.test/chat"),
                )
                raise BadRequestError(
                    "Tool choice is none, but model called a tool",
                    response=response,
                    body={"error": {"code": "tool_use_failed"}},
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="safe answer"))]
            )

    completions = RetryCompletions()
    adapter = GroqLLMClient(
        fake_client(completions),  # type: ignore[arg-type]
        model="model",
        temperature=0,
        max_completion_tokens=100,
    )

    result = await adapter.complete([ChatMessage(role="user", content="question")])

    assert result == "safe answer"
    assert len(completions.calls) == 2
    assert "No tools are available" in completions.calls[1]["messages"][0]["content"]
