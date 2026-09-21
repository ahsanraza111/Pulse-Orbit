from __future__ import annotations

from dataclasses import dataclass

from groq import AsyncGroq

from pulse.application.services.chat import ChatService
from pulse.core.config import Settings
from pulse.infrastructure.llm.groq_client import GroqLLMClient


@dataclass(frozen=True, slots=True)
class Container:
    """Application composition root. All concrete dependencies are wired here."""

    settings: Settings
    chat_service: ChatService

    @classmethod
    def build(cls, settings: Settings) -> Container:
        settings.validate_runtime()
        assert settings.groq_api_key is not None

        groq_sdk = AsyncGroq(
            api_key=settings.groq_api_key.get_secret_value(),
            timeout=settings.groq_timeout_seconds,
        )
        llm_client = GroqLLMClient(
            groq_sdk,
            model=settings.groq_model,
            temperature=settings.groq_temperature,
            max_completion_tokens=settings.groq_max_completion_tokens,
        )
        chat_service = ChatService(
            llm_client=llm_client,
            system_prompt=settings.system_prompt,
            max_user_message_chars=settings.max_user_message_chars,
        )
        return cls(settings=settings, chat_service=chat_service)

