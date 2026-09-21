from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from groq import AsyncGroq

from pulse.application.errors import ProviderError
from pulse.application.models import ChatMessage

logger = logging.getLogger(__name__)


class GroqLLMClient:
    def __init__(
        self,
        client: AsyncGroq,
        *,
        model: str,
        temperature: float,
        max_completion_tokens: int,
    ) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature
        self._max_completion_tokens = max_completion_tokens

    async def complete(self, messages: Sequence[ChatMessage]) -> str:
        try:
            completion: Any = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": message.role, "content": message.content}
                    for message in messages
                ],
                temperature=self._temperature,
                max_completion_tokens=self._max_completion_tokens,
            )
            content = completion.choices[0].message.content
            if not content or not content.strip():
                raise ProviderError("Groq returned an empty response")
            return content.strip()
        except ProviderError:
            raise
        except Exception as exc:
            logger.exception("Groq completion failed")
            raise ProviderError("The AI provider is temporarily unavailable") from exc
