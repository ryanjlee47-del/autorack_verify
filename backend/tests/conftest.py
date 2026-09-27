"""Test fixtures. Tests run against a real Postgres (TEST_DATABASE_URL), because
the behaviour under test -- row locks, partial unique indexes, append-only
triggers, JSONB -- is Postgres behaviour. SQLite would test something else.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

os.environ.setdefault("TEST_DATABASE_URL", "postgresql+psycopg://postgres@localhost:5432/autorack_test")
os.environ.update(
    {
        "DATABASE_URL": os.environ["TEST_DATABASE_URL"],
        "ENVIRONMENT": "test",
        "EMAIL_BACKEND": "memory",
        "FRONTEND_URL": "https://app.autorack.test",
        "CORS_ORIGINS": "https://app.autorack.test",
        "SECRET_KEY": "test-secret-key-test-secret-key-test-secret-key",
        "STRIPE_SECRET_KEY": "sk_test_dummy",
        "STRIPE_PRICE_ID": "price_test_175",
        "STRIPE_WEBHOOK_SECRET": "whsec_test_secret",
        "SIGNUP_ENABLED": "true",
        "GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "test-google-secret",
        "OPERATOR_EMAILS": "",
    }
)

from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from alembic import command
from autorack.db import get_engine, get_sessionmaker
from autorack.main import create_app
from autorack.services import email, google_auth
from autorack.services.ratelimit import memory_limiter

# Smallest thing that passes the upload's JPEG signature check.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Iterator[None]:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = Config(os.path.join(BACKEND, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND, "alembic"))
    cfg.attributes["skip_logging"] = True
    command.upgrade(cfg, "head")
    yield


TABLES = [
    "error_events",
    "oauth_states",
    "agreement_signatures",
    "feature_usage",
    "notifications_sent",
    "photos",
    "memberships",
    "rate_limit_hits",
    "stripe_events",
    "audit_log",
    "barcode_aliases",
    "order_flags",
    "scan_events",
    "order_line_items",
    "order_insert_checks",
    "packages",
    "restock_tasks",
    "orders",
    "pick_batches",
    "pack_inserts",
    "product_substitutes",
    "kit_components",
    "product_barcodes",
    "product_images",
    "products",
    "clients",
    "shifts",
    "integrations",
    "import_batches",
    "worker_sessions",
    "workers",
    "devices",
    "owner_sessions",
    "magic_link_tokens",
    "users",
    "warehouses",
]


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    with get_engine().begin() as conn:
        # TRUNCATE does not fire row-level DELETE triggers, so append-only
        # tables can be reset between tests.
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
    email.outbox.clear()
    memory_limiter.reset()


@pytest.fixture
def db() -> Iterator[Any]:
    s = get_sessionmaker()()
    yield s
    s.close()


@pytest.fixture(scope="session")
def app() -> Any:
    return create_app()


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Scenario helpers
# ---------------------------------------------------------------------------


@dataclass
class Owner:
    token: str
    email: str
    warehouse_id: str

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@dataclass
class Phone:
    device_token: str
    session_token: str | None = None
    session_id: str | None = None
    worker_id: str | None = None

    @property
    def h(self) -> dict[str, str]:
        headers = {"X-Device-Token": self.device_token}
        if self.session_token:
            headers["Authorization"] = f"Bearer {self.session_token}"
        return headers


# ---------------------------------------------------------------------------
# A stand-in for Google. The authorization code is "email:<address>" (or
# "email:<address>|sub:<id>"); the token endpoint answers with an ID token
# for that address, as Google would after the person picked their account.
# ---------------------------------------------------------------------------


def fake_id_token(claims: dict[str, Any]) -> str:
    def part(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(claims)}.c2ln"


class FakeGoogle:
    def __init__(self) -> None:
        self.overrides: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        code = form.get("code", "")
        if not code.startswith("email:") or not form.get("code_verifier"):
            return httpx.Response(400, json={"error": "invalid_grant"})
        addr, _, sub = code[len("email:") :].partition("|sub:")
        claims = {
            "iss": "https://accounts.google.com",
            "aud": form.get("client_id"),
            "sub": sub or f"google-{addr}",
            "email": addr,
            "email_verified": True,
            "name": addr.split("@")[0].title(),
            "exp": int(datetime.now(UTC).timestamp()) + 600,
            **self.overrides,
        }
        return httpx.Response(200, json={"id_token": fake_id_token(claims), "access_token": "x"})


@pytest.fixture(autouse=True)
def fake_google(monkeypatch: pytest.MonkeyPatch) -> FakeGoogle:
    fake = FakeGoogle()
    monkeypatch.setattr(google_auth, "TRANSPORT", httpx.MockTransport(fake))
    return fake


def google_redirect(client: TestClient, start_url: str, addr: str, sub: str | None = None) -> str:
    """Walk the browser through Google; returns where the callback sent it."""
    if start_url.startswith("http"):
        u = urllib.parse.urlparse(start_url)
        start_url = u.path + (f"?{u.query}" if u.query else "")
    r = client.get(start_url, follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    if "#error=" in loc:
        return loc
    state = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["state"][0]
    code = f"email:{addr}" + (f"|sub:{sub}" if sub else "")
    r = client.get("/api/auth/google/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert r.status_code == 302, r.text
    return r.headers["location"]


def fragment(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).fragment))


def google_login(client: TestClient, addr: str, start: str = "/api/auth/google/start", sub: str | None = None) -> str:
    """Sign in with Google as `addr`; returns the dashboard session token."""
    loc = google_redirect(client, start, addr, sub)
    frag = fragment(loc)
    assert "token" in frag, frag
    r = client.post("/api/auth/verify", json={"token": frag["token"]})
    assert r.status_code == 200, r.text
    return str(r.json()["token"])


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


AGREEMENT = {
    "signer_name": "Pat Owner",
    "signer_title": "Owner",
    "company_name": "Acme Logistics LLC",
    "company_address": "1 Dock St, Oakland, CA 94607",
    "agreement_version": "v1",
    "accept_agreement": True,
    "viewed_seconds": 95,
}


def sign_agreement(client: TestClient, owner: Owner) -> dict[str, Any]:
    """Sign for the warehouse the owner is looking at (existing or new ones)."""
    r = client.post("/api/agreement/sign", json=AGREEMENT, headers=owner.h)
    assert r.status_code == 200, r.text
    return r.json()


def signup(client: TestClient, name: str = "Acme Warehouse", addr: str | None = None) -> Owner:
    addr = addr or f"owner-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post(
        "/api/auth/signup",
        json={"warehouse_name": name, "timezone": "America/Chicago", **AGREEMENT},
    )
    assert r.status_code == 201, r.text
    token = google_login(client, addr, start=r.json()["redirect"])
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    return Owner(token=token, email=addr, warehouse_id=me["warehouse"]["id"])


def add_worker(client: TestClient, owner: Owner, name: str = "Maria", pin: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name}
    if pin:
        body["pin"] = pin
    r = client.post("/api/workers", json=body, headers=owner.h)
    assert r.status_code == 201, r.text
    return r.json()


def link_phone(client: TestClient, owner: Owner, label: str = "Phone 1") -> Phone:
    code = client.get("/api/warehouse/device-link", headers=owner.h).json()["join_code"]
    r = client.post("/api/worker/link", json={"join_code": code, "label": label})
    assert r.status_code == 200, r.text
    return Phone(device_token=r.json()["device_token"])


def login(client: TestClient, phone: Phone, pin: str) -> Phone:
    r = client.post("/api/worker/login", json={"pin": pin}, headers=phone.h)
    assert r.status_code == 200, r.text
    data = r.json()
    phone.session_token = data["session_token"]
    phone.session_id = data["session_id"]
    phone.worker_id = data["worker"]["id"]
    if data.get("notice_required"):
        r = client.post("/api/worker/notice", json={"version": data["notice_version"]}, headers=phone.h)
        assert r.status_code == 200, r.text
    return phone


def worker_on_phone(client: TestClient, owner: Owner, name: str = "Maria") -> Phone:
    w = add_worker(client, owner, name)
    return login(client, link_phone(client, owner, f"{name}'s phone"), w["pin"])


def make_order(
    client: TestClient, owner: Owner, lines: list[tuple[str, int]] | None = None, number: str | None = None
) -> dict[str, Any]:
    lines = lines or [("012345678905", 2), ("036000291452", 1)]
    r = client.post(
        "/api/orders",
        json={
            "external_order_number": number or f"SO-{uuid.uuid4().hex[:6]}",
            "lines": [{"barcode": b, "quantity": q, "sku": f"SKU-{i}"} for i, (b, q) in enumerate(lines)],
        },
        headers=owner.h,
    )
    assert r.status_code == 201, r.text
    return r.json()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def scan_event(phone: Phone, order_id: str, barcode: str, **extra: Any) -> dict[str, Any]:
    ev = {
        "id": str(uuid.uuid4()),
        "kind": "scan",
        "order_id": order_id,
        "session_id": phone.session_id,
        "client_scanned_at": now_iso(),
        "scanned_barcode": barcode,
    }
    ev.update(extra)
    return ev


def sync(client: TestClient, phone: Phone, *events: dict[str, Any], expect: int = 200) -> dict[str, Any]:
    r = client.post("/api/worker/sync", json={"events": list(events)}, headers={"X-Device-Token": phone.device_token})
    assert r.status_code == expect, r.text
    return r.json()


def scan(client: TestClient, phone: Phone, order_id: str, barcode: str, **extra: Any) -> dict[str, Any]:
    out = sync(client, phone, scan_event(phone, order_id, barcode, **extra))
    ev = out["events"][0]
    return {**ev, "order": out["orders"].get(order_id)}
