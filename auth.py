"""Password hashing and web session helpers for the owner app.

PBKDF2-SHA256 (stdlib hashlib, no extra dependency), 200k iterations.
Sessions are opaque tokens in our own web_sessions table (not Flask's
signed cookie session) so they can be listed/revoked server-side and so
the impersonation-link flow (operator GUI -> owner web login) can create
one the same way a normal login does.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta

import db

SESSION_COOKIE_NAME = "session_token"
SESSION_LIFETIME_HOURS = 12
PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2$sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


# Not a password: the character set generated passwords are drawn from.
# 0/O and 1/l/I are excluded because an operator reads these aloud or
# retypes them from a screenshot when relaying a reset by hand.
RELAY_PASSWORD_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"  # noqa: S105


def generate_relay_password(length: int = 14) -> str:
    """A random password meant to be manually relayed back to an account
    owner (by email reply) after an operator-mediated password reset --
    see admin_gui.py's AccountsTab. No ambiguous characters, since a
    human will be reading and typing this, not a password manager."""
    return "".join(secrets.choice(RELAY_PASSWORD_ALPHABET) for _ in range(length))


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, _, iterations, salt_hex, hash_hex = encoded.split("$")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    return hmac.compare_digest(dk, expected)


def _expiry(hours: int = SESSION_LIFETIME_HOURS) -> str:
    """When a session issued now should stop being accepted.

    Milliseconds, not microseconds. This value is compared against
    strftime('%Y-%m-%dT%H:%M:%fZ','now') in SQL and against app.py's
    _now_iso() in Python -- both of which emit MILLISECONDS -- and the
    comparison is a plain string comparison, not a date comparison. A
    microsecond string sorts *below* the millisecond string for the same
    instant ('...123456Z' < '...123Z', because '4' < 'Z'), so an expiry
    written at microsecond precision reads as already elapsed. Every
    producer of an ISO instant in this codebase must use this format.
    """
    return (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def start_session(conn, user_id: int, account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.create_web_session(conn, user_id, account_id, token, _expiry())
    return token


def current_user(conn, token: str | None):
    if not token:
        return None
    row = db.get_web_session(conn, token)
    if not row:
        return None
    user = db.get_user(conn, row["user_id"])
    account = db.get_account(conn, row["account_id"])
    if not user or not account:
        return None
    return {"user": user, "account": account}


def end_session(conn, token: str | None) -> None:
    if token:
        db.delete_web_session(conn, token)
