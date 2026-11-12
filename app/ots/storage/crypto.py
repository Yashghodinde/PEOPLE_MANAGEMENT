"""Encrypt small secrets kept in the database (the pre-authenticated uploadUrl) with OTS_FIELD_ENCRYPTION_KEY.

The key is a Fernet key: 32 random bytes, URL-safe base64. Generate one with
    python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

from .errors import ConfigurationError


def _fernet() -> Fernet:
    key = getattr(settings, "OTS_FIELD_ENCRYPTION_KEY", "")
    if not key:
        raise ConfigurationError("OTS_FIELD_ENCRYPTION_KEY is not set: an upload URL cannot be stored safely")
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise ConfigurationError("OTS_FIELD_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode() if text else ""


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:  # wrong key or damaged value: treat the session as lost, not as a crash
        raise ConfigurationError("a stored upload URL cannot be decrypted with the current key") from exc
