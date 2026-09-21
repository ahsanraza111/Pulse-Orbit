from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import FastAPI
from microsoft_teams.apps import App, FastAPIAdapter

from pulse.application.errors import InvalidMessageError, ProviderError
from pulse.application.services.chat import ChatService
from pulse.core.config import Settings

logger = logging.getLogger(__name__)
MENTION_PATTERN = re.compile(r"<at>.*?</at>", flags=re.IGNORECASE | re.DOTALL)


def clean_teams_text(text: str | None) -> str:
    return MENTION_PATTERN.sub("", text or "").strip()


class TeamsMessageHandler:
    def __init__(self, chat_service: ChatService) -> None:
        self._chat_service = chat_service

    async def __call__(self, ctx: Any) -> None:
        user_text = clean_teams_text(getattr(ctx.activity, "text", None))
        try:
            response = await self._chat_service.reply(user_text)
        except InvalidMessageError as exc:
            await ctx.send(str(exc))
        except ProviderError:
            await ctx.send("PULSE cannot reach the AI service right now. Please try again shortly.")
        except Exception:
            logger.exception("Unexpected error while handling a Teams message")
            await ctx.send("Something went wrong while processing your message. Please try again.")
        else:
            await ctx.send(response)


def register_teams_app(
    fastapi_app: FastAPI,
    settings: Settings,
    chat_service: ChatService,
) -> App:
    adapter = FastAPIAdapter(app=fastapi_app)
    kwargs: dict[str, Any] = {
        "http_server_adapter": adapter,
        "dangerously_allow_unauthenticated_requests": settings.teams_skip_auth,
    }
    if not settings.teams_skip_auth:
        kwargs.update(
            client_id=settings.teams_client_id,
            client_secret=(
                settings.teams_client_secret.get_secret_value()
                if settings.teams_client_secret
                else None
            ),
            tenant_id=settings.teams_tenant_id,
        )

    teams_app = App(**kwargs)
    teams_app.on_message(TeamsMessageHandler(chat_service))
    return teams_app
