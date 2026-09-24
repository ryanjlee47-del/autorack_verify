"""Credential primitives: opaque tokens, PIN hashing, PIN fingerprints.

Tokens (magic links, owner sessions, device tokens, worker sessions) are
random, opaque, and stored only as SHA-256 hashes. A database leak therefore
yields nothing that can be replayed.

PINs get two representations:

* `pin_hash`: salted PBKDF2, the thing a login is verified against.
* `pin_fingerprint`: HMAC-SHA256(SECRET_KEY, warehouse_id:pin). A 4-digit PIN
  has only 10,000 values, so a salted hash alone cannot stop an offline guess
  if the table leaks; the keyed fingerprint can, as long as SECRET_KEY lives
  somewhere other than the database. It also lets a PIN-only login find its
  worker in one indexed lookup, and makes "unique per warehouse" a database
  constraint instead of a convention.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid

from .config import get_settings

PBKDF2_ITERATIONS = 120_000
# 31 symbols: no 0/O, 1/I/L, so a join code read aloud or off a wall survives.
JOIN_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

# PINs nobody should be issued. Owners may still choose them deliberately.
WEAK_PINS = {"0000", "1111", "2222", "3333", "4444", "5555", "6666", "7777", "8888", "9999", "1234", "4321"}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_join_code() -> str:
    raw = "".join(secrets.choice(JOIN_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_join_code(code: str) -> str:
    cleaned = "".join(ch for ch in code.upper() if ch.isalnum())
    # Humans type 0 for O and 1 for I; the alphabet contains neither.
    cleaned = cleaned.replace("0", "O").replace("1", "I")
    if len(cleaned) != 8:
        return cleaned
    return f"{cleaned[:4]}-{cleaned[4:]}"


def is_valid_pin(pin: str) -> bool:
    return len(pin) == get_settings().pin_length and pin.isascii() and pin.isdigit()


def generate_pin(taken: set[str] | None = None) -> str:
    taken = taken or set()
    length = get_settings().pin_length
    for _ in range(1000):
        pin = "".join(secrets.choice("0123456789") for _ in range(length))
        if pin not in WEAK_PINS and pin not in taken:
            return pin
    raise RuntimeError("Could not generate an unused PIN")


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), base64.b64decode(salt_b64), int(iterations))
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def pin_fingerprint(warehouse_id: uuid.UUID, pin: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, f"{warehouse_id}:{pin}".encode(), hashlib.sha256).hexdigest()
