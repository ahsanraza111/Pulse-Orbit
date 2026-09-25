from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import URL, DateTime, Index, String, Text, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pulse.core.config import Settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class OrbitSessionRecord(Base):
    __tablename__ = "orbit_sessions"
    __table_args__ = (
        Index("ix_orbit_sessions_session_expires_at", "session_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    teams_user_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    provider_token_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    session_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


def build_database_url(settings: Settings) -> URL:
    if not settings.database_is_configured:
        raise ValueError("PostgreSQL configuration is incomplete")
    assert settings.database_host is not None
    assert settings.database_name is not None
    assert settings.database_user is not None
    assert settings.database_password is not None
    return URL.create(
        drivername="postgresql+asyncpg",
        username=settings.database_user,
        password=settings.database_password.get_secret_value(),
        host=settings.database_host,
        port=settings.database_port,
        database=settings.database_name,
    )


@dataclass(frozen=True, slots=True)
class Database:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    @classmethod
    def build(cls, settings: Settings) -> Database:
        engine = create_async_engine(
            build_database_url(settings),
            pool_pre_ping=True,
            pool_recycle=1800,
            hide_parameters=True,
        )
        return cls(
            engine=engine,
            sessions=async_sessionmaker(engine, expire_on_commit=False),
        )

    async def is_ready(self) -> bool:
        try:
            async with self.sessions() as database_session:
                await database_session.execute(select(OrbitSessionRecord.id).limit(1))
        except SQLAlchemyError:
            logger.exception("PostgreSQL readiness check failed")
            return False
        return True

    async def close(self) -> None:
        await self.engine.dispose()
