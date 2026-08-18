"""Shared pytest setup.

The owner app requires a CSRF token on every cookie-authenticated write
(see app.py's `_require_csrf_token`). Rather than thread that token
through the dozens of existing route tests by hand, the fixture below
teaches the Flask test client to attach the caller's own token
automatically -- the same thing a real browser does by submitting the
hidden field rendered into every owner form.

Injection is deliberately skipped when the test already supplies a
`csrf_token` key, so a test can still pass a missing/blank/wrong token to
prove the check rejects it. See tests/test_csrf.py, which does exactly
that.
"""

from __future__ import annotations

import flask.testing
import pytest

import auth


@pytest.fixture(autouse=True)
def auto_csrf_token(monkeypatch):
    original_post = flask.testing.FlaskClient.post

    def post(self, *args, **kwargs):
        data = kwargs.get("data")
        # `json=` bodies are the worker PWA's sync endpoint, which is
        # CSRF-exempt -- leave those alone. Everything else is a form
        # post, including the several routes that take no fields at all
        # (revoke, toggle-active, confirm-reject) and so arrive here with
        # no `data` kwarg.
        if "json" not in kwargs and (data is None or isinstance(data, dict)):  # noqa: SIM102
            if not (isinstance(data, dict) and "csrf_token" in data):
                token = _token_for(self)
                if token:
                    # Copy rather than mutate: several tests reuse the
                    # same dict across two posts to assert idempotency.
                    kwargs["data"] = {**(data or {}), "csrf_token": token}
        return original_post(self, *args, **kwargs)

    monkeypatch.setattr(flask.testing.FlaskClient, "post", post)


def _token_for(client) -> str:
    """The CSRF token bound to whatever session cookie this client holds,
    or "" if it isn't logged in (nothing to bind to, nothing to send)."""
    app = client.application
    token_fn = getattr(app, "csrf_token", None)
    session_cookie = client.get_cookie(auth.SESSION_COOKIE_NAME)
    if token_fn is None or session_cookie is None:
        return ""
    headers = {"Cookie": f"{auth.SESSION_COOKIE_NAME}={session_cookie.value}"}
    with app.test_request_context(headers=headers):
        return token_fn()
