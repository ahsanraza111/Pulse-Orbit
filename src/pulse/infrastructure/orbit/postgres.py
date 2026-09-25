from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pulse.application.errors import OrbitAuthRequiredError
from pulse.application.orbit_models import OrbitSession
from pulse.infrastructure.database import OrbitSessionRecord
from pulse.infrastructure.orbit.encryption import OrbitTokenCipher


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class PostgresOrbitSessionStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: OrbitTokenCipher,
        *,
        ttl_minutes: int,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._cipher = cipher
        self._ttl = timedelta(minutes=ttl_minutes)
        self._now = now

    async def get(self, teams_user_id: str) -> OrbitSession | None:
        now = as_utc(self._now())
        async with self._sessions() as database_session:
            async with database_session.begin():
                await self._delete_expired(database_session, now)
                record = await database_session.scalar(
                    select(OrbitSessionRecord)
                    .where(OrbitSessionRecord.teams_user_id == teams_user_id)
                    .with_for_update()
                )
                if record is None:
                    return None
                try:
                    access_token = self._cipher.decrypt(record.encrypted_access_token)
                    refresh_token = self._cipher.decrypt(record.encrypted_refresh_token)
                except ValueError:
                    await database_session.delete(record)
                    return None
                record.last_used_at = now
                return OrbitSession(
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_at=as_utc(record.provider_token_expires_at),
                )

    async def save(
        self,
        teams_user_id: str,
        session: OrbitSession,
        *,
        reset_ttl: bool = False,
    ) -> None:
        now = as_utc(self._now())
        encrypted_access = self._cipher.encrypt(session.access_token)
        encrypted_refresh = self._cipher.encrypt(session.refresh_token)
        try:
            await self._save_once(
                teams_user_id,
                session,
                encrypted_access,
                encrypted_refresh,
                reset_ttl=reset_ttl,
                now=now,
            )
        except IntegrityError:
            # Concurrent first logins can race on the unique Teams user ID. The
            # winning row is now lockable, so retry once as an update.
            await self._save_once(
                teams_user_id,
                session,
                encrypted_access,
                encrypted_refresh,
                reset_ttl=reset_ttl,
                now=now,
            )

    async def _save_once(
        self,
        teams_user_id: str,
        session: OrbitSession,
        encrypted_access: str,
        encrypted_refresh: str,
        *,
        reset_ttl: bool,
        now: datetime,
    ) -> None:
        async with self._sessions() as database_session:
            async with database_session.begin():
                await self._delete_expired(database_session, now)
                record = await database_session.scalar(
                    select(OrbitSessionRecord)
                    .where(OrbitSessionRecord.teams_user_id == teams_user_id)
                    .with_for_update()
                )
                if record is None:
                    if not reset_ttl:
                        raise OrbitAuthRequiredError(
                            "Your PULSE Orbit session expired. Please sign in again."
                        )
                    record = OrbitSessionRecord(
                        id=str(uuid4()),
                        teams_user_id=teams_user_id,
                        encrypted_access_token=encrypted_access,
                        encrypted_refresh_token=encrypted_refresh,
                        provider_token_expires_at=as_utc(session.expires_at),
                        session_expires_at=now + self._ttl,
                        created_at=now,
                        updated_at=now,
                        last_used_at=None,
                    )
                    database_session.add(record)
                    return

                record.encrypted_access_token = encrypted_access
                record.encrypted_refresh_token = encrypted_refresh
                record.provider_token_expires_at = as_utc(session.expires_at)
                record.updated_at = now
                if reset_ttl:
                    record.created_at = now
                    record.session_expires_at = now + self._ttl
                    record.last_used_at = None

    async def delete(self, teams_user_id: str) -> None:
        async with self._sessions() as database_session:
            async with database_session.begin():
                await database_session.execute(
                    delete(OrbitSessionRecord).where(
                        OrbitSessionRecord.teams_user_id == teams_user_id
                    )
                )

    async def cleanup_expired(self) -> int:
        now = as_utc(self._now())
        async with self._sessions() as database_session:
            async with database_session.begin():
                return await self._delete_expired(database_session, now)

    @staticmethod
    async def _delete_expired(
        database_session: AsyncSession,
        now: datetime,
    ) -> int:
        result = await database_session.execute(
            delete(OrbitSessionRecord).where(OrbitSessionRecord.session_expires_at <= now)
        )
        return int(result.rowcount or 0)
