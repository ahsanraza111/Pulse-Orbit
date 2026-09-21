from collections.abc import Sequence
from typing import Protocol

from pulse.application.models import ChatMessage


class LLMClient(Protocol):
    async def complete(self, messages: Sequence[ChatMessage]) -> str:
        """Return a single assistant response for the ordered messages."""
        ...
