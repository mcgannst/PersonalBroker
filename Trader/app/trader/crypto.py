"""Fernet encryption for secrets at rest (SPEC §14)."""

from cryptography.fernet import Fernet, InvalidToken


class Crypto:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, token: str | None) -> str | None:
        """Plain text, or None when the token is empty or can't be read with this key."""
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            return None
