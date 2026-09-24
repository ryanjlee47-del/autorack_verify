"""Owner magic-link auth, and worker device + PIN auth."""

from __future__ import annotations

from datetime import timedelta

from conftest import add_worker, last_link_token, link_phone, login, signup
from sqlalchemy import update

from autorack.models import MagicLinkToken, utcnow
from autorack.services import email


def test_signup_sends_link_and_link_signs_in(client):
    owner = signup(client, "North Dock")
    me = client.get("/api/auth/me", headers=owner.h).json()
    assert me["warehouse"]["name"] == "North Dock"
    assert me["warehouse"]["timezone"] == "America/Chicago"
    assert me["user"]["role"] == "owner"
    assert me["access"]["state"] == "trialing"
    assert me["access"]["trial_days_left"] == 14


def test_magic_link_is_single_use(client):
    owner = signup(client)
    client.post("/api/auth/magic-link", json={"email": owner.email})
    token = last_link_token(owner.email)
    assert client.post("/api/auth/verify", json={"token": token}).status_code == 200
    r = client.post("/api/auth/verify", json={"token": token})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "link_invalid"


def test_magic_link_expires(client, db):
    owner = signup(client)
    client.post("/api/auth/magic-link", json={"email": owner.email})
    token = last_link_token(owner.email)
    db.execute(update(MagicLinkToken).values(expires_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    assert client.post("/api/auth/verify", json={"token": token}).status_code == 401


def test_magic_link_url_keeps_token_out_of_server_logs(client):
    owner = signup(client)
    client.post("/api/auth/magic-link", json={"email": owner.email})
    body = email.outbox[-1].text
    assert "https://app.autorack.test/app/login.html#token=" in body


def test_magic_link_does_not_reveal_unknown_emails(client):
    r = client.post("/api/auth/magic-link", json={"email": "nobody@example.com"})
    assert r.status_code == 200
    assert not [m for m in email.outbox if m.to == "nobody@example.com"]


def test_magic_link_rate_limited_per_email(client):
    owner = signup(client)
    codes = [client.post("/api/auth/magic-link", json={"email": owner.email}).status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_duplicate_signup_rejected(client):
    owner = signup(client)
    r = client.post("/api/auth/signup", json={"warehouse_name": "Again", "email": owner.email.upper()})
    assert r.status_code == 409


def test_logout_revokes_session(client):
    owner = signup(client)
    assert client.post("/api/auth/logout", headers=owner.h).status_code == 200
    assert client.get("/api/auth/me", headers=owner.h).status_code == 401


def test_unauthenticated_requests_rejected(client):
    assert client.get("/api/orders").status_code == 401
    assert client.get("/api/orders", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_signup_can_be_closed(client, monkeypatch):
    from autorack.config import get_settings

    monkeypatch.setattr(get_settings(), "signup_enabled", False)
    r = client.post("/api/auth/signup", json={"warehouse_name": "X", "email": "x@example.com"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------


def test_invite_manager_and_manager_cannot_manage_billing(client):
    owner = signup(client)
    r = client.post("/api/team", json={"email": "mgr@example.com", "role": "manager"}, headers=owner.h)
    assert r.status_code == 201
    token = last_link_token("mgr@example.com")
    mtoken = client.post("/api/auth/verify", json={"token": token}).json()["token"]
    mh = {"Authorization": f"Bearer {mtoken}"}
    assert client.get("/api/orders", headers=mh).status_code == 200
    assert client.post("/api/billing/checkout", headers=mh).status_code == 403
    assert client.patch("/api/warehouse", json={"name": "Hijack"}, headers=mh).status_code == 403


def test_cannot_remove_last_owner(client):
    owner = signup(client)
    me = client.get("/api/auth/me", headers=owner.h).json()["user"]
    r = client.patch(f"/api/team/{me['id']}", json={"active": False}, headers=owner.h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "last_owner"


# ---------------------------------------------------------------------------
# Workers and devices
# ---------------------------------------------------------------------------


def test_worker_pin_login_flow(client):
    owner = signup(client)
    w = add_worker(client, owner, "Maria")
    assert len(w["pin"]) == 4 and w["pin"].isdigit()
    phone = login(client, link_phone(client, owner), w["pin"])
    assert phone.worker_id == w["id"]
    assert client.get("/api/worker/orders", headers=phone.h).status_code == 200


def test_pin_unique_within_warehouse_but_not_across(client):
    a = signup(client, "A")
    b = signup(client, "B")
    add_worker(client, a, "One", pin="4821")
    r = client.post("/api/workers", json={"name": "Two", "pin": "4821"}, headers=a.h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "pin_taken"
    add_worker(client, b, "Other warehouse", pin="4821")  # no collision across warehouses


def test_deactivating_frees_pin_and_ends_sessions(client):
    owner = signup(client)
    w = add_worker(client, owner, "Leaver", pin="7391")
    phone = login(client, link_phone(client, owner), "7391")
    r = client.patch(f"/api/workers/{w['id']}", json={"active": False}, headers=owner.h)
    assert r.status_code == 200
    assert client.get("/api/worker/orders", headers=phone.h).status_code == 401
    add_worker(client, owner, "Newcomer", pin="7391")


def test_wrong_pin_locks_device_after_five_attempts(client):
    owner = signup(client)
    add_worker(client, owner, "Maria", pin="5555")
    phone = link_phone(client, owner)
    codes = [client.post("/api/worker/login", json={"pin": "0000"}, headers=phone.h).status_code for _ in range(5)]
    assert codes == [401] * 5
    r = client.post("/api/worker/login", json={"pin": "5555"}, headers=phone.h)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "pin_locked"


def test_bad_join_code(client):
    r = client.post("/api/worker/link", json={"join_code": "ZZZZ-ZZZZ"})
    assert r.status_code == 400


def test_join_code_is_forgiving_about_formatting(client):
    owner = signup(client)
    code = client.get("/api/warehouse/device-link", headers=owner.h).json()["join_code"]
    r = client.post("/api/worker/link", json={"join_code": code.replace("-", "").lower()})
    assert r.status_code == 200


def test_revoked_device_is_locked_out(client):
    owner = signup(client)
    w = add_worker(client, owner)
    phone = login(client, link_phone(client, owner), w["pin"])
    device_id = client.get("/api/devices", headers=owner.h).json()[0]["id"]
    client.post(f"/api/devices/{device_id}/revoke", headers=owner.h)
    r = client.get("/api/worker/device", headers=phone.h)
    assert r.status_code == 401 and r.json()["detail"]["code"] == "device_unlinked"


def test_rotating_join_code_invalidates_old_code(client):
    owner = signup(client)
    old = client.get("/api/warehouse/device-link", headers=owner.h).json()["join_code"]
    new = client.post("/api/warehouse/device-link/rotate", headers=owner.h).json()["join_code"]
    assert old != new
    assert client.post("/api/worker/link", json={"join_code": old}).status_code == 400
    assert client.post("/api/worker/link", json={"join_code": new}).status_code == 200


def test_reset_pin_signs_worker_out(client):
    owner = signup(client)
    w = add_worker(client, owner, pin="2222")
    phone = login(client, link_phone(client, owner), "2222")
    new_pin = client.post(f"/api/workers/{w['id']}/reset-pin", json={}, headers=owner.h).json()["pin"]
    assert new_pin != "2222"
    assert client.get("/api/worker/orders", headers=phone.h).status_code == 401
    login(client, phone, new_pin)
