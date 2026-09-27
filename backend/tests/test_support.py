"""'Contact support' emails the operator with the account details."""

from __future__ import annotations

from conftest import signup

from autorack.config import get_settings
from autorack.services import email


def test_contact_support_emails_with_reply_to(client, monkeypatch):
    owner = signup(client, name="Dockside")
    body = {"topic": "problem", "message": "Phone won't link <script>", "page": "#/devices"}
    assert client.post("/api/support", json=body, headers=owner.h).status_code == 503  # nowhere to send yet

    monkeypatch.setattr(get_settings(), "operator_emails", "ops@example.com")
    assert client.post("/api/support", json=body, headers=owner.h).json() == {"sent": True}
    msg = email.outbox[-1]
    assert msg.to == "ops@example.com" and msg.reply_to.startswith("owner-")
    assert "Something isn't working" in msg.subject and "Dockside" in msg.subject
    assert "#/devices" in msg.text and "&lt;script&gt;" in msg.html

    monkeypatch.setattr(get_settings(), "support_email", "help@example.com")
    client.post("/api/support", json=body, headers=owner.h)
    assert email.outbox[-1].to == "help@example.com"


def test_contact_support_is_rate_limited_and_validated(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "operator_emails", "ops@example.com")
    owner = signup(client)
    assert client.post("/api/support", json={"message": "hi"}, headers=owner.h).status_code == 422
    for _ in range(5):
        assert client.post("/api/support", json={"message": "hello there"}, headers=owner.h).status_code == 200
    assert client.post("/api/support", json={"message": "hello there"}, headers=owner.h).status_code == 429
    assert client.post("/api/support", json={"message": "hello there"}).status_code == 401
