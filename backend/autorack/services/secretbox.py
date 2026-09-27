"""Encrypting the store credentials Autorack keeps (API tokens and keys).

Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from SECRET_KEY, so a
database dump alone doesn't hand out anyone's Shopify token. Rotating
SECRET_KEY makes existing credentials unreadable: the integrations then show
"reconnect" and the owner pastes their key again.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings


class SecretUnreadable(Exception):
    pass


def _fernet() -> Fernet:
    digest = hashlib.sha256(b"autorack-integrations|" + get_settings().secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def seal(data: dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def unseal(token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    try:
        return dict(json.loads(_fernet().decrypt(token.encode())))
    except (InvalidToken, ValueError) as exc:
        raise SecretUnreadable("stored credentials can't be decrypted; reconnect") from exc
