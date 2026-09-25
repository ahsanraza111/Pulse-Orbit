from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from pulse.application.orbit_models import OrbitSession
from pulse.infrastructure.orbit.memory import EncryptedInMemoryOrbitSessionStore


@pytest.mark.asyncio
async def test_session_store_encrypts_tokens_in_memory() -> None:
    store = EncryptedInMemoryOrbitSessionStore(Fernet.generate_key().decode())
    original = OrbitSession(
        access_token="sensitive-access-token",
        refresh_token="sensitive-refresh-token",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    await store.save("teams-user", original)

    encrypted = store._values["teams-user"]
    assert b"sensitive-access-token" not in encrypted
    assert await store.get("teams-user") == original

