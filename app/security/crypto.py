"""Encrypt and decrypt OAuth tokens at rest with Fernet (AES + HMAC)."""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.errors import NeedsReconnect


@lru_cache
def _fernet() -> Fernet:
    """Build the Fernet cipher from TOKEN_ENCRYPTION_KEY once."""
    if not settings.token_encryption_key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set.")
    return Fernet(settings.token_encryption_key.encode())


def encrypt(plain_text: str) -> str:
    """Encrypt a secret for storage."""
    return _fernet().encrypt(plain_text.encode()).decode()


def decrypt(cipher_text: str) -> str:
    """Decrypt a stored secret. A token we cannot decrypt is treated as a lost connection."""
    try:
        return _fernet().decrypt(cipher_text.encode()).decode()
    except InvalidToken as error:
        # Happens if TOKEN_ENCRYPTION_KEY was rotated: the user must sign in to Google again.
        raise NeedsReconnect() from error
