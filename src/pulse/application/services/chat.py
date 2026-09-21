from dataclasses import dataclass

from pulse.application.errors import InvalidMessageError
from pulse.application.models import ChatMessage
from pulse.application.ports.llm import LLMClient


@dataclass(slots=True)
class ChatService:
    llm_client: LLMClient
    system_prompt: str
    max_user_message_chars: int

    async def reply(self, user_text: str) -> str:
        normalized = user_text.strip()
        if not normalized:
            raise InvalidMessageError("Please send a text message so I can help.")
        if len(normalized) > self.max_user_message_chars:
            raise InvalidMessageError(
                f"Your message is too long. Please keep it under "
                f"{self.max_user_message_chars} characters."
            )

        return await self.llm_client.complete(
            (
                ChatMessage(role="system", content=self.system_prompt),
                ChatMessage(role="user", content=normalized),
            )
        )

