from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pulse.application.orbit_models import OrbitSession
from pulse.infrastructure.database import Base, OrbitSessionRecord
from pulse.infrastructure.orbit.encryption import OrbitTokenCipher
from pulse.infrastructure.orbit.postgres import PostgresOrbitSessionStore


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 25, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, *, minutes: int) -> None:
        self.value += timedelta(minutes=minutes)


def orbit_session(*, suffix: str = "one") -> OrbitSession:
    return OrbitSession(
        access_token=f"access-{suffix}",
        refresh_token=f"refresh-{suffix}",
        expires_at=datetime(2026, 9, 25, 12, 30, tzinfo=UTC),
    )


@pytest.fixture
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_is_encrypted_and_survives_store_recreation(database) -> None:
    clock = Clock()
    cipher = OrbitTokenCipher(Fernet.generate_key().decode())
    first_store = PostgresOrbitSessionStore(
        database, cipher, ttl_minutes=60, now=clock.now
    )

    await first_store.save("teams-user-1", orbit_session(), reset_ttl=True)

    async with database() as database_session:
        record = await database_session.scalar(select(OrbitSessionRecord))
        assert record is not None
        assert "access-one" not in record.encrypted_access_token
        assert "refresh-one" not in record.encrypted_refresh_token

    second_store = PostgresOrbitSessionStore(
        database, cipher, ttl_minutes=60, now=clock.now
    )
    assert await second_store.get("teams-user-1") == orbit_session()


@pytest.mark.asyncio
async def test_provider_refresh_does_not_extend_absolute_session(database) -> None:
    clock = Clock()
    store = PostgresOrbitSessionStore(
        database,
        OrbitTokenCipher(Fernet.generate_key().decode()),
        ttl_minutes=60,
        now=clock.now,
    )
    await store.save("teams-user-1", orbit_session(), reset_ttl=True)

    clock.advance(minutes=20)
    refreshed = OrbitSession(
        access_token="access-two",
        refresh_token="refresh-two",
        expires_at=clock.now() + timedelta(hours=1),
    )
    await store.save("teams-user-1", refreshed, reset_ttl=False)

    async with database() as database_session:
        record = await database_session.scalar(select(OrbitSessionRecord))
        assert record is not None
        assert record.session_expires_at.replace(tzinfo=UTC) == datetime(
            2026, 9, 25, 13, tzinfo=UTC
        )
    assert await store.get("teams-user-1") == refreshed


@pytest.mark.asyncio
async def test_expired_session_is_deleted(database) -> None:
    clock = Clock()
    store = PostgresOrbitSessionStore(
        database,
        OrbitTokenCipher(Fernet.generate_key().decode()),
        ttl_minutes=60,
        now=clock.now,
    )
    await store.save("teams-user-1", orbit_session(), reset_ttl=True)

    clock.advance(minutes=60)

    assert await store.get("teams-user-1") is None
    async with database() as database_session:
        assert await database_session.scalar(select(OrbitSessionRecord)) is None


@pytest.mark.asyncio
async def test_explicit_login_replaces_session_and_resets_ttl(database) -> None:
    clock = Clock()
    store = PostgresOrbitSessionStore(
        database,
        OrbitTokenCipher(Fernet.generate_key().decode()),
        ttl_minutes=60,
        now=clock.now,
    )
    await store.save("teams-user-1", orbit_session(), reset_ttl=True)

    clock.advance(minutes=15)
    replacement = OrbitSession(
        access_token="replacement-access",
        refresh_token="replacement-refresh",
        expires_at=clock.now() + timedelta(hours=1),
    )
    await store.save("teams-user-1", replacement, reset_ttl=True)

    async with database() as database_session:
        record = await database_session.scalar(select(OrbitSessionRecord))
        assert record is not None
        assert record.session_expires_at.replace(tzinfo=UTC) == datetime(
            2026, 9, 25, 13, 15, tzinfo=UTC
        )
    assert await store.get("teams-user-1") == replacement


@pytest.mark.asyncio
async def test_logout_deletes_persisted_session(database) -> None:
    clock = Clock()
    store = PostgresOrbitSessionStore(
        database,
        OrbitTokenCipher(Fernet.generate_key().decode()),
        ttl_minutes=60,
        now=clock.now,
    )
    await store.save("teams-user-1", orbit_session(), reset_ttl=True)

    await store.delete("teams-user-1")

    assert await store.get("teams-user-1") is None


@pytest.mark.asyncio
async def test_session_encrypted_with_another_key_is_deleted(database) -> None:
    clock = Clock()
    original_store = PostgresOrbitSessionStore(
        database,
        OrbitTokenCipher(Fernet.generate_key().decode()),
        ttl_minutes=60,
        now=clock.now,
    )
    await original_store.save("teams-user-1", orbit_session(), reset_ttl=True)

    replacement_store = PostgresOrbitSessionStore(
        database,
        OrbitTokenCipher(Fernet.generate_key().decode()),
        ttl_minutes=60,
        now=clock.now,
    )

    assert await replacement_store.get("teams-user-1") is None
    async with database() as database_session:
        assert await database_session.scalar(select(OrbitSessionRecord)) is None
