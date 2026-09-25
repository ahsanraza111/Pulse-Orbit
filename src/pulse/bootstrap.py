from __future__ import annotations

from dataclasses import dataclass

import httpx
from groq import AsyncGroq

from pulse.application.services.chat import ChatService
from pulse.application.services.orbit import (
    OrbitAddEntryService,
    OrbitAuthService,
    OrbitViewEntriesService,
)
from pulse.core.config import Settings
from pulse.infrastructure.database import Database
from pulse.infrastructure.llm.groq_client import GroqLLMClient
from pulse.infrastructure.llm.timesheet_parser import LLMTimesheetDraftParser
from pulse.infrastructure.llm.timesheet_query_parser import LLMTimesheetQueryParser
from pulse.infrastructure.orbit.encryption import OrbitTokenCipher
from pulse.infrastructure.orbit.memory import InMemoryPendingEntryStore
from pulse.infrastructure.orbit.postgres import PostgresOrbitSessionStore
from pulse.infrastructure.orbit.supabase import SupabaseOrbitClient


@dataclass(frozen=True, slots=True)
class Container:
    """Application composition root. All concrete dependencies are wired here."""

    settings: Settings
    chat_service: ChatService
    orbit_auth_service: OrbitAuthService | None = None
    orbit_add_service: OrbitAddEntryService | None = None
    orbit_view_service: OrbitViewEntriesService | None = None
    orbit_http_client: httpx.AsyncClient | None = None
    database: Database | None = None

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
        if not settings.orbit_is_configured:
            return cls(settings=settings, chat_service=chat_service)

        assert settings.orbit_supabase_url is not None
        assert settings.orbit_supabase_anon_key is not None
        assert settings.orbit_session_encryption_key is not None

        database = Database.build(settings)
        orbit_http = httpx.AsyncClient(timeout=settings.orbit_http_timeout_seconds)
        orbit_client = SupabaseOrbitClient(
            orbit_http,
            base_url=settings.orbit_supabase_url,
            api_key=settings.orbit_supabase_anon_key.get_secret_value(),
        )
        session_store = PostgresOrbitSessionStore(
            database.sessions,
            OrbitTokenCipher(settings.orbit_session_encryption_key.get_secret_value()),
            ttl_minutes=settings.orbit_session_ttl_minutes,
        )
        auth_service = OrbitAuthService(
            orbit_client,
            session_store,
        )
        add_service = OrbitAddEntryService(
            auth_service,
            orbit_client,
            LLMTimesheetDraftParser(llm_client),
            InMemoryPendingEntryStore(),
            confirmation_ttl_seconds=settings.orbit_confirmation_ttl_seconds,
            business_timezone=settings.orbit_business_timezone,
            max_duration_minutes=settings.orbit_max_duration_minutes,
            max_notes_chars=settings.orbit_max_notes_chars,
        )
        view_service = OrbitViewEntriesService(
            auth_service,
            orbit_client,
            LLMTimesheetQueryParser(llm_client),
            business_timezone=settings.orbit_business_timezone,
            page_size=settings.orbit_view_page_size,
            max_range_days=settings.orbit_view_max_range_days,
        )
        return cls(
            settings=settings,
            chat_service=chat_service,
            orbit_auth_service=auth_service,
            orbit_add_service=add_service,
            orbit_view_service=view_service,
            orbit_http_client=orbit_http,
            database=database,
        )

    async def close(self) -> None:
        if self.orbit_http_client:
            await self.orbit_http_client.aclose()
        if self.database:
            await self.database.close()
