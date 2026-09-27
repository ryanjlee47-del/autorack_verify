"""Owner Sign in with Google, and worker device + PIN auth."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from conftest import AGREEMENT, add_worker, fragment, google_login, google_redirect, link_phone, login, signup
from sqlalchemy import select, update

from autorack.models import MagicLinkToken, User, utcnow
from autorack.services import email


def test_signup_sends_link_and_link_signs_in(client):
    owner = signup(client, "North Dock")
    me = client.get("/api/auth/me", headers=owner.h).json()
    assert me["warehouse"]["name"] == "North Dock"
    assert me["warehouse"]["timezone"] == "America/Chicago"
    assert me["user"]["role"] == "owner"
    assert me["access"]["state"] == "trialing"
    assert me["access"]["trial_days_left"] == 14


def test_login_code_is_single_use_and_expires(client, db):
    owner = signup(client)
    loc = google_redirect(client, "/api/auth/google/start", owner.email)
    code = fragment(loc)["token"]
    assert loc.startswith("https://app.autorack.test/app/login.html#token=")  # never sent to a server log
    assert client.post("/api/auth/verify", json={"token": code}).status_code == 200
    r = client.post("/api/auth/verify", json={"token": code})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "link_invalid"
    code2 = fragment(google_redirect(client, "/api/auth/google/start", owner.email))["token"]
    db.execute(update(MagicLinkToken).values(expires_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    assert client.post("/api/auth/verify", json={"token": code2}).status_code == 401


def test_google_start_uses_pkce_and_select_account(client):
    r = client.get("/api/auth/google/start?next=%23/orders", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["client_id"] == ["test-client.apps.googleusercontent.com"]
    assert q["redirect_uri"] == ["https://app.autorack.test/api/auth/google/callback"]
    assert q["code_challenge_method"] == ["S256"] and q["prompt"] == ["select_account"]
    assert q["scope"] == ["openid email profile"]
    cookie = r.headers["set-cookie"]
    assert "ar_oauth_state=" in cookie and "HttpOnly" in cookie and "Path=/api/auth/google" in cookie


def test_next_page_is_kept_and_open_redirects_are_not(client):
    owner = signup(client)
    loc = google_redirect(client, "/api/auth/google/start?next=%23/orders/abc", owner.email)
    assert fragment(loc)["next"] == "#/orders/abc"
    loc = google_redirect(client, "/api/auth/google/start?next=https://evil.example", owner.email)
    assert "next" not in fragment(loc)


def test_callback_needs_the_state_cookie_from_this_browser(client, fake_google):
    owner = signup(client)
    r = client.get("/api/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    client.cookies.clear()  # the callback lands in a different browser
    fake_google.requests.clear()
    r = client.get(
        "/api/auth/google/callback", params={"code": f"email:{owner.email}", "state": state}, follow_redirects=False
    )
    assert fragment(r.headers["location"])["error"] == "state_mismatch"
    assert fake_google.requests == []  # the code was never even exchanged


def test_state_is_single_use(client):
    owner = signup(client)
    r = client.get("/api/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    params = {"code": f"email:{owner.email}", "state": state}
    first = client.get("/api/auth/google/callback", params=params, follow_redirects=False)
    assert "token" in fragment(first.headers["location"])
    client.cookies.set("ar_oauth_state", state, path="/api/auth/google")
    again = client.get("/api/auth/google/callback", params=params, follow_redirects=False)
    assert fragment(again.headers["location"])["error"] == "expired"


def test_tokens_for_another_app_or_unverified_emails_are_refused(client, fake_google):
    owner = signup(client)
    fake_google.overrides = {"aud": "someone-else.apps.googleusercontent.com"}
    assert fragment(google_redirect(client, "/api/auth/google/start", owner.email))["error"] == "google_failed"
    fake_google.overrides = {"email_verified": False}
    assert fragment(google_redirect(client, "/api/auth/google/start", owner.email))["error"] == "email_unverified"
    fake_google.overrides = {"iss": "https://evil.example"}
    assert fragment(google_redirect(client, "/api/auth/google/start", owner.email))["error"] == "google_failed"


def test_account_is_bound_to_the_first_google_account(client, db):
    owner = signup(client)  # first sign-in bound google-<email>
    user = db.scalar(select(User).where(User.email == owner.email))
    assert user.google_sub == f"google-{owner.email}"
    loc = google_redirect(client, "/api/auth/google/start", owner.email, sub="someone-else")
    assert fragment(loc)["error"] == "account_mismatch"


def test_unknown_email_gets_no_account_and_no_user(client, db):
    loc = google_redirect(client, "/api/auth/google/start", "stranger@example.com")
    frag = fragment(loc)
    assert frag["error"] == "no_account" and frag["email"] == "stranger@example.com"
    assert db.scalar(select(User).where(User.email == "stranger@example.com")) is None


def test_cancelled_at_google(client):
    r = client.get("/api/auth/google/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert fragment(r.headers["location"])["error"] == "cancelled"


def test_there_is_no_email_sign_in(client, app):
    assert client.post("/api/auth/magic-link", json={"email": "x@example.com"}).status_code in (404, 405)
    assert "/api/auth/magic-link" not in app.openapi()["paths"]


def test_duplicate_signup_rejected(client):
    owner = signup(client)
    r = client.post("/api/auth/signup", json={"warehouse_name": "Again", **AGREEMENT})
    loc = google_redirect(client, r.json()["redirect"], owner.email.upper().lower())
    assert fragment(loc)["error"] == "account_exists"


def test_signup_ticket_is_single_use(client):
    r = client.post("/api/auth/signup", json={"warehouse_name": "Once", **AGREEMENT})
    url = r.json()["redirect"]
    signup_email = "once@example.com"
    assert "token" in fragment(google_redirect(client, url, signup_email))
    assert fragment(google_redirect(client, url, "twice@example.com"))["error"] == "expired"


def test_google_not_configured(client, monkeypatch):
    from autorack.config import get_settings

    monkeypatch.setattr(get_settings(), "google_client_id", "")
    r = client.get("/api/auth/google/start", follow_redirects=False)
    assert fragment(r.headers["location"])["error"] == "google_not_configured"
    r = client.post("/api/auth/signup", json={"warehouse_name": "X", **AGREEMENT})
    assert r.status_code == 503


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
    r = client.post("/api/auth/signup", json={"warehouse_name": "X", **AGREEMENT})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------


def test_invite_manager_and_manager_cannot_manage_billing(client):
    owner = signup(client)
    r = client.post("/api/team", json={"email": "mgr@example.com", "role": "manager"}, headers=owner.h)
    assert r.status_code == 201
    invite = [m for m in email.outbox if m.to == "mgr@example.com"][-1]
    assert "Sign in with Google" in invite.text and "https://app.autorack.test/app/login.html" in invite.text
    mtoken = google_login(client, "mgr@example.com")
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
