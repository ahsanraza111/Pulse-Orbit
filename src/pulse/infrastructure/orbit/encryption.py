from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class OrbitTokenCipher:
    def __init__(self, encryption_key: str) -> None:
        try:
            self._fernet = Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError(
                "PULSE_ORBIT_SESSION_ENCRYPTION_KEY is not a valid Fernet key"
            ) from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
            raise ValueError("Orbit session ciphertext cannot be decrypted") from exc
