from __future__ import annotations

import asyncio
import json
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

from pulse.application.orbit_models import OrbitSession, PendingTimesheetEntry


class EncryptedInMemoryOrbitSessionStore:
    """Process-local MVP store; replace with durable encrypted storage in production."""

    def __init__(self, encryption_key: str) -> None:
        try:
            self._fernet = Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError(
                "PULSE_ORBIT_SESSION_ENCRYPTION_KEY is not a valid Fernet key"
            ) from exc
        self._values: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def get(self, teams_user_id: str) -> OrbitSession | None:
        async with self._lock:
            encrypted = self._values.get(teams_user_id)
        if not encrypted:
            return None
        try:
            payload = json.loads(self._fernet.decrypt(encrypted).decode("utf-8"))
            return OrbitSession(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                expires_at=datetime.fromisoformat(payload["expires_at"]),
            )
        except (InvalidToken, KeyError, ValueError, json.JSONDecodeError):
            await self.delete(teams_user_id)
            return None

    async def save(self, teams_user_id: str, session: OrbitSession) -> None:
        payload = json.dumps(
            {
                "access_token": session.access_token,
                "refresh_token": session.refresh_token,
                "expires_at": session.expires_at.isoformat(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        encrypted = self._fernet.encrypt(payload)
        async with self._lock:
            self._values[teams_user_id] = encrypted

    async def delete(self, teams_user_id: str) -> None:
        async with self._lock:
            self._values.pop(teams_user_id, None)


class InMemoryPendingEntryStore:
    def __init__(self) -> None:
        self._values: dict[str, PendingTimesheetEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, teams_user_id: str) -> PendingTimesheetEntry | None:
        async with self._lock:
            return self._values.get(teams_user_id)

    async def save(self, entry: PendingTimesheetEntry) -> None:
        async with self._lock:
            self._values[entry.teams_user_id] = entry

    async def delete(self, teams_user_id: str) -> None:
        async with self._lock:
            self._values.pop(teams_user_id, None)
