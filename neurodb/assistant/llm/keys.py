"""The API keys entered in the admin's AI settings, sealed so that the database never holds them in clear.

A key is sealed with Fernet under a key derived from the site's ``SECRET_KEY``; the keys in
``SECRET_KEY_FALLBACKS`` still open it, so a rotation that keeps the old secret there keeps the keys
readable, and the next save seals them again with the new one (:func:`reseal`). After a change of
``SECRET_KEY`` without the old one in the fallbacks a sealed key can no longer be read: AI settings then
says "enter the key again". A key the hosting team sets in the environment is never stored.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.utils.crypto import salted_hmac

SALT = "neurodb.assistant.ai-keys"


def _fernet() -> MultiFernet:
    """One Fernet per secret key, the current one first (it seals; any of them opens)."""
    secrets = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])]
    return MultiFernet(
        [
            Fernet(
                base64.urlsafe_b64encode(salted_hmac(SALT, "fernet", secret=s, algorithm="sha256").digest())
            )
            for s in secrets
        ]
    )


def seal(text: str) -> str:
    """``text`` sealed (a Fernet token, as text)."""
    return _fernet().encrypt(text.encode()).decode()


def unseal(token: str) -> str | None:
    """The text of a sealed ``token``; None when there is none or it cannot be read (the secret key
    changed, or the token is damaged)."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None


def reseal(token: str) -> str | None:
    """``token`` sealed again with the current secret key (it may have been sealed under one in
    ``SECRET_KEY_FALLBACKS``); None when it cannot be read."""
    if not token:
        return None
    try:
        return _fernet().rotate(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None


def last4(text: str) -> str:
    """The end of a key that AI settings shows ("ending …wxyz")."""
    return (text or "")[-4:]
