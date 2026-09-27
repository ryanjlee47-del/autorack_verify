"""Sign in with Google: the only way into the dashboard.

The flow (OAuth 2.0 authorization code + PKCE, OpenID Connect):

1. `/api/auth/google/start` records an attempt (a random state, a PKCE
   verifier, and for a sign-up the warehouse details and signed agreement),
   sets the same state in a short-lived HttpOnly cookie, and redirects to
   Google.
2. Google sends the browser back to `/api/auth/google/callback` with a code.
   The state must match both the stored attempt and the cookie (so nobody
   can finish a sign-in in someone else's browser).
3. The code is exchanged, server to server, for Google's ID token, which
   names the verified email. The token comes straight from Google's token
   endpoint over TLS, which OpenID Connect accepts in place of checking its
   signature; its issuer, audience, expiry and email_verified are checked.
4. The person is matched to their Autorack account by email (and, after the
   first time, by Google's account id), and the browser is sent to the
   sign-in page with a one-time code that it swaps for a session, exactly as
   before. Nobody gets an account by signing in: they sign up, are invited,
   or are an operator.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import MagicLinkToken, OAuthState, User, utcnow
from ..security import hash_token, new_token
from . import audit
from .audit import Actor

log = logging.getLogger("autorack.google")

STATE_TTL = timedelta(minutes=15)
LOGIN_CODE_TTL = timedelta(minutes=2)
COOKIE = "ar_oauth_state"
ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

# Tests swap this for httpx.MockTransport.
TRANSPORT: httpx.BaseTransport | None = None


class GoogleError(Exception):
    """`code` goes to the sign-in page, which shows a matching message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class GoogleIdentity:
    sub: str
    email: str
    name: str | None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def safe_next(value: str | None) -> str | None:
    """Only in-app hash routes ("#/orders/..."): never an open redirect."""
    if value and value.startswith("#/") and len(value) <= 200 and "\n" not in value:
        return value
    return None


def begin(
    db: Session,
    *,
    intent: str,
    payload: dict[str, Any] | None = None,
    next_path: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str]:
    """Record an attempt. Returns (Google URL to redirect to, state for the cookie)."""
    s = get_settings()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)[:96]
    db.add(
        OAuthState(
            state_hash=hash_token(state),
            code_verifier=verifier,
            intent=intent,
            payload=payload or {},
            next_path=safe_next(next_path),
            ip=ip,
            user_agent=(user_agent or "")[:300] or None,
            expires_at=utcnow() + STATE_TTL,
        )
    )
    db.flush()
    params = {
        "client_id": s.google_client_id,
        "redirect_uri": s.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
        "prompt": "select_account",
        "access_type": "online",
    }
    return f"{s.google_auth_url}?{urlencode(params)}", state


def take_state(db: Session, state: str) -> OAuthState:
    now = utcnow()
    row = db.scalar(select(OAuthState).where(OAuthState.state_hash == hash_token(state)).with_for_update())
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise GoogleError("expired", "That sign-in took too long or was already used. Try again.")
    row.used_at = now
    db.flush()
    return row


def _claims(id_token: str) -> dict[str, Any]:
    try:
        body = id_token.split(".")[1]
        return dict(json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))))
    except (IndexError, ValueError) as exc:
        raise GoogleError("google_failed", "Google's answer couldn't be read. Try again.") from exc


def exchange(code: str, verifier: str) -> GoogleIdentity:
    s = get_settings()
    try:
        with httpx.Client(timeout=15, transport=TRANSPORT) as c:
            r = c.post(
                s.google_token_url,
                data={
                    "code": code,
                    "client_id": s.google_client_id,
                    "client_secret": s.google_client_secret,
                    "redirect_uri": s.google_redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise GoogleError("google_failed", "Couldn't reach Google. Try again in a moment.") from exc
    if r.status_code != 200:
        log.warning("google token exchange failed: %s %s", r.status_code, r.text[:300])
        raise GoogleError("google_failed", "Google didn't accept the sign-in. Try again.")
    token = (r.json() or {}).get("id_token")
    if not token:
        raise GoogleError("google_failed", "Google didn't say who you are. Try again.")
    c = _claims(token)
    if c.get("iss") not in ISSUERS or c.get("aud") != s.google_client_id:
        raise GoogleError("google_failed", "That sign-in wasn't meant for Autorack.")
    if int(c.get("exp") or 0) < utcnow().timestamp():
        raise GoogleError("expired", "That sign-in expired. Try again.")
    if not c.get("email") or c.get("email_verified") not in (True, "true"):
        raise GoogleError("email_unverified", "Your Google account's email isn't verified yet.")
    return GoogleIdentity(sub=str(c["sub"]), email=str(c["email"]).strip().lower(), name=c.get("name"))


def bind(db: Session, user: User, who: GoogleIdentity) -> None:
    """Tie the account to this Google account on first use; refuse another."""
    if user.google_sub and user.google_sub != who.sub:
        raise GoogleError(
            "account_mismatch",
            f"{who.email} is linked to a different Google account. Sign in with that one, or ask us to reset it.",
        )
    if not user.google_sub:
        user.google_sub = who.sub
    if not user.name and who.name:
        user.name = who.name[:200]


def login_code(db: Session, user: User, ip: str | None, ttl: timedelta = LOGIN_CODE_TTL) -> str:
    """A one-time code the sign-in page swaps for a session (POST /auth/verify)."""
    code = new_token()
    db.add(MagicLinkToken(user_id=user.id, token_hash=hash_token(code), expires_at=utcnow() + ttl, requested_ip=ip))
    db.flush()
    return code


def finish_url(code: str, next_path: str | None) -> str:
    base = f"{get_settings().frontend_url.rstrip('/')}/app/login.html#token={quote(code)}"
    return base + (f"&next={quote(next_path)}" if next_path else "")


def error_url(err: GoogleError, email: str | None = None) -> str:
    params = {"error": err.code, "message": err.message}
    if email:
        params["email"] = email
    return f"{get_settings().frontend_url.rstrip('/')}/app/login.html#{urlencode(params)}"


def audit_login(db: Session, user: User, ip: str | None) -> None:
    audit.record(
        db,
        Actor("user", str(user.id), user.email, ip),
        "user.google_signin",
        warehouse_id=user.warehouse_id,
        target_type="user",
        target_id=user.id,
    )


def prune(db: Session) -> int:
    n = db.execute(delete(OAuthState).where(OAuthState.expires_at < utcnow() - timedelta(days=1))).rowcount
    return int(n or 0)
