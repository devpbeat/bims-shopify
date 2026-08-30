"""Fernet-based symmetric encryption helper for tenant secrets at rest."""
from __future__ import annotations

from cryptography.fernet import Fernet


class SecretBox:
    def __init__(self, key: str) -> None:
        if not key:
            key = Fernet.generate_key().decode("utf-8")
        self._fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
