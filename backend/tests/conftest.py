"""Test fixtures. Tests run against a real Postgres (TEST_DATABASE_URL), because
the behaviour under test -- row locks, partial unique indexes, append-only
triggers, JSONB -- is Postgres behaviour. SQLite would test something else.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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
    }
)

from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from alembic import command
from autorack.db import get_engine, get_sessionmaker
from autorack.main import create_app
from autorack.services import email
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
    "orders",
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


def last_link_token(to: str) -> str:
    for msg in reversed(email.outbox):
        if msg.to == to:
            m = re.search(r"#token=([A-Za-z0-9_\-%]+)", msg.text)
            assert m, msg.text
            return m.group(1)
    raise AssertionError(f"no email to {to}")


def signup(client: TestClient, name: str = "Acme Warehouse", addr: str | None = None) -> Owner:
    addr = addr or f"owner-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/auth/signup", json={"warehouse_name": name, "email": addr, "timezone": "America/Chicago"})
    assert r.status_code == 201, r.text
    r = client.post("/api/auth/verify", json={"token": last_link_token(addr)})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
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
