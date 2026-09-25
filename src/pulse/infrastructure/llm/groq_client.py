from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from groq import AsyncGroq, BadRequestError

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
            return await self._complete_once(messages)
        except BadRequestError as exc:
            if not self._is_unexpected_tool_call(exc):
                logger.exception("Groq rejected the completion request")
                raise ProviderError("The AI provider rejected the request") from exc
            logger.warning("Groq attempted an unavailable tool; retrying once without tools")
            guarded = (
                ChatMessage(
                    role="system",
                    content=(
                        "No tools are available. Never call or reference a tool, function, "
                        "repository browser, or external action. Respond only with the plain "
                        "text or JSON requested by the following instructions."
                    ),
                ),
                *messages,
            )
            try:
                return await self._complete_once(guarded)
            except Exception as retry_exc:
                logger.exception("Groq completion retry failed")
                raise ProviderError("The AI provider is temporarily unavailable") from retry_exc
        except ProviderError:
            raise
        except Exception as exc:
            logger.exception("Groq completion failed")
            raise ProviderError("The AI provider is temporarily unavailable") from exc

    async def _complete_once(self, messages: Sequence[ChatMessage]) -> str:
        completion: Any = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            temperature=self._temperature,
            max_completion_tokens=self._max_completion_tokens,
            tool_choice="none",
        )
        content = completion.choices[0].message.content
        if not content or not content.strip():
            raise ProviderError("Groq returned an empty response")
        return content.strip()

    @staticmethod
    def _is_unexpected_tool_call(exc: BadRequestError) -> bool:
        detail = str(getattr(exc, "body", "")) + str(exc)
        return "tool_use_failed" in detail or "Tool choice is none" in detail
